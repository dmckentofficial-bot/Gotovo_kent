import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import HTTPException

from app import auth, database, main, pin, telegram, worker


class ParserTests(unittest.TestCase):
    def test_extracts_hidden_and_visible_links_and_media(self):
        source = """
        <div class="tgme_widget_message_wrap" data-post="source/7">
          <div class="tgme_widget_message_text">Save <a href="https://shop.example/item?a=1&amp;b=2">here</a>;
          <a href="https://example.org/">https://example.org/</a><br>last line</div>
          <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo.jpg')"></a>
        </div>
        """
        parser = telegram.PublicFeedParser()
        parser.feed(source)
        self.assertEqual(parser.posts[0]["id"], "7")
        self.assertIn("https://shop.example/item?a=1&b=2", parser.posts[0]["text"])
        self.assertEqual(parser.posts[0]["text"].count("https://example.org/"), 1)
        self.assertEqual(parser.posts[0]["media"][0]["type"], "photo")


class MessageTests(unittest.TestCase):
    def _bot_response(self):
        class Response:
            ok = True
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}

        return Response()

    def test_splits_long_unicode_text_without_losing_characters(self):
        content = "Начало 😀 " + ("длинный текст " * 900) + "Конец 🔚"
        pieces = telegram.split_text(content, telegram.MAX_MESSAGE_UNITS)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), content)
        self.assertTrue(all(telegram.text_units(piece) <= telegram.MAX_MESSAGE_UNITS for piece in pieces))

    def test_long_photo_caption_continues_as_messages(self):
        content = "Начало " + ("подробное описание " * 110) + "Конец"
        with patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post({"text": content, "media": [{"type": "photo", "url": "unused"}]}, "@dest")
            requests = [call.kwargs["data"] for call in send.call_args_list]

        delivered = requests[0]["caption"] + "".join(item["text"] for item in requests[1:])
        self.assertEqual(delivered, content)
        self.assertLessEqual(telegram.text_units(requests[0]["caption"]), telegram.MAX_CAPTION_UNITS)

    def test_photo_album_sent_as_single_media_group(self):
        content = "Альбом из трёх фото"
        media = [{"type": "photo", "url": f"https://cdn4.telesco.pe/file/{index}.jpg"} for index in range(3)]
        with patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            message = telegram.publish_post({"text": content, "media": media}, "@dest")

        self.assertTrue(message)
        calls = send.call_args_list
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in calls], ["sendMediaGroup"])
        self.assertEqual(len(calls), 1)
        media_json = json.loads(calls[0].kwargs["data"]["media"])
        self.assertEqual(len(media_json), 3)
        self.assertEqual(media_json[0]["caption"], content)
        self.assertNotIn("caption", media_json[1])

    def test_album_larger_than_ten_split_into_several_groups(self):
        media = [{"type": "photo", "url": f"https://cdn4.telesco.pe/file/{index}.jpg"} for index in range(12)]
        with patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post({"text": "", "media": media}, "@dest")

        calls = send.call_args_list
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in calls], ["sendMediaGroup", "sendMediaGroup"])
        self.assertEqual([len(json.loads(call.kwargs["data"]["media"])) for call in calls], [10, 2])

    def test_signature_is_appended_to_text_post(self):
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post({"text": "Новость", "media": []}, "@dest", "Подпишитесь: @channel")

        text = send.call_args.kwargs["data"]["text"]
        self.assertEqual(text, "Новость\n\nПодпишитесь: @channel")

    def test_signature_is_appended_to_photo_caption(self):
        with patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post(
                {"text": "Фото дня", "media": [{"type": "photo", "url": "unused"}]},
                "@dest",
                "Наш канал",
            )

        data = send.call_args_list[0].kwargs["data"]
        self.assertIn("caption", data)
        self.assertEqual(data["caption"], "Фото дня\n\nНаш канал")

    def test_signature_only_post_without_media(self):
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post({"text": "", "media": []}, "@dest", "Только подпись")

        self.assertEqual(send.call_args.kwargs["data"]["text"], "Только подпись")

    def test_empty_signature_leaves_text_untouched(self):
        post = {"text": "Без изменений", "media": []}
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post(post, "@dest", "")

        self.assertEqual(send.call_args.kwargs["data"]["text"], "Без изменений")

    def test_publish_post_uses_copy_message_for_full_volume(self):
        # Пост с привязкой к источнику обязан идти через copyMessage,
        # а не через скачивание/перезалив (там терялись текст и качество).
        post = {"id": "777", "source_username": "some_channel", "text": "snippet", "media": []}
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            result = telegram.publish_post(post, "@dest", "")

        self.assertTrue(result)
        methods = [call.args[0].rsplit("/", 1)[-1] for call in send.call_args_list]
        self.assertEqual(methods, ["copyMessage"])
        data = send.call_args_list[0].kwargs["data"]
        self.assertEqual(data["from_chat_id"], "@some_channel")
        self.assertEqual(data["message_id"], 777)
        self.assertEqual(data["chat_id"], "@dest")

    def test_forward_message_falls_back_to_forward_on_copy_error(self):
        class FailOnce:
            def __init__(self):
                self.calls = 0

            def __call__(self, *args, **kwargs):
                self.calls += 1
                method = args[0].rsplit("/", 1)[-1] if args else ""
                if method == "copyMessage":
                    raise RuntimeError("Telegram Bot API (400): message can't be copied")
                class Response:
                    ok = True
                    status_code = 200

                    def json(self):
                        return {"ok": True, "result": {"message_id": 1}}
                return Response()

        responder = FailOnce()
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", side_effect=responder):
            result = telegram.forward_message("@dest", "@src", 5)

        self.assertTrue(result)
        self.assertEqual(responder.calls, 2)

    def test_signature_is_merged_into_copied_text_via_edit(self):
        copied = {"message_id": 10, "text": "Оригинал", "entities": []}
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram._apply_signature("@dest", copied, "Подпись")

        data = send.call_args.kwargs["data"]
        self.assertEqual(data["text"], "Оригинал\n\nПодпись")

    def test_fetch_single_post_returns_matching_id(self):
        html = """
        <div class="tgme_widget_message_wrap" data-post="chan/5">
          <div class="tgme_widget_message_text">Короткий</div>
        </div>
        <div class="tgme_widget_message_wrap" data-post="chan/9">
          <div class="tgme_widget_message_text">Полный текст девятого поста</div>
        </div>
        """

        class FakeResponse:
            status_code = 200
            text = html

            def raise_for_status(self):
                pass

        with patch("app.telegram.requests.get", return_value=FakeResponse()):
            post = telegram.fetch_single_post("chan", 9)

        self.assertEqual(post["id"], "9")
        self.assertEqual(post["text"], "Полный текст девятого поста")

    def test_parser_preserves_html_formatting_and_links(self):
        source = """
        <div class="tgme_widget_message_wrap" data-post="source/9">
          <div class="tgme_widget_message_text"><b>Жирный</b> и <i>курсив</i>,
          <a href="https://example.org/page">читать тут</a><br>конец</div>
        </div>
        """
        parser = telegram.PublicFeedParser()
        parser.feed(source)
        post = parser.posts[0]
        self.assertIn("<b>Жирный</b>", post["html"])
        self.assertIn("<i>курсив</i>", post["html"])
        self.assertIn('<a href="https://example.org/page">читать тут</a>', post["html"])
        # plain-text остаётся для фильтра и истории
        self.assertIn("Жирный", post["text"])
        self.assertIn("читать тут", post["text"])

    def test_upload_fallback_sends_html_with_parse_mode(self):
        post = {
            "text": "Жирный и ссылка",
            "html": '<b>Жирный</b> и <a href="https://example.org/">ссылка</a>',
            "media": [],
        }
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            telegram.publish_post(post, "@dest", "")

        data = send.call_args.kwargs["data"]
        self.assertEqual(data.get("parse_mode"), "HTML")
        self.assertIn("<b>Жирный</b>", data["text"])

    def test_publish_post_falls_back_to_full_upload_when_copy_impossible(self):
        # Бота нет в чужом канале: copyMessage невозможен — пост обязан
        # дойти целиком через перезаливку, а не потеряться с ошибкой.
        post = {
            "id": "42",
            "source_username": "alien_channel",
            "text": "Полный текст",
            "media": [{"type": "photo", "url": "https://cdn4.telesco.pe/file/x.jpg"}],
        }
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "forward_message", side_effect=RuntimeError("message to copy not found")), \
             patch.object(telegram, "fetch_single_post", return_value=dict(post)), \
             patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch("app.telegram.requests.post", return_value=self._bot_response()) as send:
            result = telegram.publish_post(post, "@dest", "")

        self.assertTrue(result)
        methods = [call.args[0].rsplit("/", 1)[-1] for call in send.call_args_list]
        self.assertIn("sendPhoto", methods)
        self.assertNotIn("copyMessage", methods)


class PinTests(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        stored = pin.hash_pin("1234")
        self.assertTrue(stored.startswith("pbkdf2$"))
        self.assertTrue(pin.verify_pin(stored, "1234"))
        self.assertFalse(pin.verify_pin(stored, "9999"))

    def test_hash_is_different_each_time(self):
        self.assertNotEqual(pin.hash_pin("1234"), pin.hash_pin("1234"))

    def test_verify_rejects_garbage(self):
        self.assertFalse(pin.verify_pin("", "1234"))
        self.assertFalse(pin.verify_pin("not-a-hash", "1234"))
        self.assertFalse(pin.verify_pin(None, "1234"))

    def test_lockout_starts_after_free_attempts(self):
        for fails in range(pin.LOCK_FREE_ATTEMPTS):
            self.assertEqual(pin.lockout_seconds(fails), 0)
        self.assertGreater(pin.lockout_seconds(pin.LOCK_FREE_ATTEMPTS + 1), 0)
        self.assertLessEqual(pin.lockout_seconds(100), pin.LOCK_MAX_SECONDS)


class SignatureConfigTests(unittest.TestCase):
    def test_default_config_has_signature_fields(self):
        config = database.DEFAULT_CONFIG
        self.assertIn("signature_enabled", config)
        self.assertIn("signature_text", config)
        self.assertFalse(config["signature_enabled"])
        self.assertEqual(config["signature_text"], "")

    def test_worker_passes_signature_to_publish_post(self):
        config = {
            "sources": ["source_one"],
            "destination": "@destination",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
            "signature_enabled": True,
            "signature_text": "Подписывайтесь: @example",
            "enabled": True,
        }
        passed = {}
        with patch.object(database, "get_config", return_value=config), \
             patch.object(database, "get_cursor", return_value=1), \
             patch.object(database, "set_cursor"), \
             patch.object(database, "already_forwarded", return_value=False), \
             patch.object(database, "write_history"), \
             patch.object(database, "set_state"), \
             patch.object(telegram, "fetch_public_posts",
                          return_value=[{"id": "2", "text": "Новый пост", "media": []}]), \
             patch.object(telegram, "publish_post",
                          side_effect=lambda post, destination, signature="": passed.update(signature=signature) or True), \
             patch.object(telegram, "BOT_TOKEN", "123:test"):
            worker.poll_once()

        self.assertEqual(passed, {"signature": "Подписывайтесь: @example"})


class PinEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp_dir.name) / "test.sqlite3"
        database.initialize()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.tmp_dir.cleanup()

    def _user(self):
        return {"id": 42, "first_name": "Owner"}

    def test_setup_verify_change_disable_flow(self):
        from app.main import (
            PinChangePayload,
            PinPayload,
            pin_change,
            pin_disable,
            pin_setup,
            pin_status,
            pin_verify,
        )

        self.assertEqual(pin_status(_user=None), {"enabled": False})

        pin_setup(PinPayload(pin="1234"), _user=None)
        self.assertTrue(pin_status(_user=None)["enabled"])

        with self.assertRaises(HTTPException):
            pin_verify(PinPayload(pin="0000"), _user=None)

        self.assertTrue(pin_verify(PinPayload(pin="1234"), _user=None)["ok"])

        pin_change(PinChangePayload(current_pin="1234", pin="5678"), _user=None)
        self.assertTrue(pin_verify(PinPayload(pin="5678"), _user=None)["ok"])

        pin_disable(PinPayload(pin="5678"), _user=None)
        self.assertFalse(pin_status(_user=None)["enabled"])

    def test_setup_refuses_second_pin(self):
        from app.main import PinPayload, pin_setup

        pin_setup(PinPayload(pin="1234"), _user=None)
        with self.assertRaises(HTTPException):
            pin_setup(PinPayload(pin="9999"), _user=None)


class RetryTests(unittest.TestCase):
    class Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.ok = status < 400
            self.status_code = status

        def json(self):
            return self.payload

    def test_retries_on_429_then_succeeds(self):
        responses = iter([
            self.Response({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 1}}, status=429),
            self.Response({"ok": True, "result": {"id": 1}}),
        ])
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", side_effect=lambda *args, **kwargs: next(responses)) as send, \
             patch("app.telegram.time.sleep") as sleep:
            result = telegram.bot_request("getMe")

        self.assertEqual(result["id"], 1)
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once()

    def test_retries_on_5xx_then_succeeds(self):
        responses = iter([
            self.Response({"ok": False, "error_code": 502, "description": "Bad Gateway"}, status=502),
            self.Response({"ok": True, "result": {"ok": True}}),
        ])
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", side_effect=lambda *args, **kwargs: next(responses)), \
             patch("app.telegram.time.sleep") as sleep:
            telegram.bot_request("sendMessage", {"chat_id": "x", "text": "y"})
        self.assertEqual(sleep.call_count, 1)

    def test_permanent_error_is_not_retried(self):
        responses = iter([
            self.Response({"ok": False, "error_code": 400, "description": "Bad Request"}, status=400),
        ])
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", side_effect=lambda *args, **kwargs: next(responses)) as send, \
             patch("app.telegram.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                telegram.bot_request("sendMessage", {"chat_id": "x", "text": "y"})
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()


class AuthTests(unittest.TestCase):
    def test_accepts_valid_signed_admin_init_data(self):
        token = "123456:sample-test-token"
        user = {"id": 987654321, "first_name": "Admin"}
        values = {"auth_date": "2000000000", "user": json.dumps(user, separators=(",", ":"))}
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token, "ADMIN_TELEGRAM_ID": str(user["id"])}), \
             patch("app.auth.time.time", return_value=2000000010):
            result = auth.validate_init_data(urlencode(values))
        self.assertEqual(result["id"], user["id"])

    def test_accepts_valid_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "4322", "ADMIN_TELEGRAM_ID": "8967870222"}):
            result = auth.authenticate("", "4322")
        self.assertEqual(result["auth_method"], "key")
        self.assertEqual(result["id"], 8967870222)

    def test_rejects_wrong_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "4322", "ADMIN_TELEGRAM_ID": "8967870222"}):
            with self.assertRaisesRegex(PermissionError, "пароль доступа"):
                auth.authenticate("", "0000")

    def test_empty_credentials_are_rejected(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "4322", "ADMIN_TELEGRAM_ID": "8967870222"}):
            with self.assertRaisesRegex(PermissionError, "пароль доступа"):
                auth.authenticate("", "")


class AdminKeyLockoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp_dir.name) / "test.sqlite3"
        database.initialize()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.tmp_dir.cleanup()

    def test_lockout_after_five_failed_attempts(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "4322", "ADMIN_TELEGRAM_ID": "8967870222"}):
            for _ in range(5):
                with self.assertRaises(HTTPException) as ctx:
                    main.require_admin("", "0000")
                self.assertEqual(ctx.exception.status_code, 401)
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin("", "0000")
            self.assertEqual(ctx.exception.status_code, 429)
            # правильный пароль работает даже при блокировке
            user = main.require_admin("", "4322")
            self.assertEqual(user["auth_method"], "password")
            self.assertEqual(database.get_state("key_locked_until"), "0")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp_dir.name) / "test.sqlite3"
        database.initialize()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.tmp_dir.cleanup()

    def test_history_stats_counts_statuses(self):
        post = {"id": "1", "text": "привет", "media": []}
        database.write_history(post, "source", "forwarded")
        database.write_history(post, "source", "forwarded")
        database.write_history(post, "source", "filtered")
        database.write_history(post, "source", "error")
        stats = database.history_stats()
        self.assertEqual(stats["forwarded"], 2)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["total"], 4)

    def test_history_stats_empty(self):
        self.assertEqual(database.history_stats()["total"], 0)

    def test_already_forwarded_detects_duplicates(self):
        forwarded = {"id": "42", "text": "уже ушло", "media": []}
        self.assertFalse(database.already_forwarded("source", "42"))
        database.write_history(forwarded, "source", "forwarded")
        self.assertTrue(database.already_forwarded("source", "42"))

        filtered = {"id": "43", "text": "не прошло фильтр", "media": []}
        database.write_history(filtered, "source", "filtered")
        self.assertFalse(database.already_forwarded("source", "43"))


class WorkerTests(unittest.TestCase):
    def test_destination_must_be_a_real_channel(self):
        config = {
            "sources": ["source_one"],
            "destination": "8967870222",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
            "enabled": True,
        }
        with patch.object(database, "get_config", return_value=config), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "fetch_public_posts", return_value=[{"id": "1", "text": "x", "media": []}]), \
             patch.object(telegram, "bot_request", side_effect=lambda name, data=None: {
                 "getMe": {"ok": True, "id": 42, "username": "bot_name"},
                 "getChat": {"ok": True, "type": "private", "title": "", "username": ""},
             }.get(name, {})):
            with self.assertRaisesRegex(RuntimeError, "настоящим Telegram-каналом"):
                worker.validate_configuration()

    def test_destination_channel_requires_admin(self):
        config = {
            "sources": ["source_one"],
            "destination": "@real_channel",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
            "enabled": True,
        }
        with patch.object(database, "get_config", return_value=config), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "fetch_public_posts", return_value=[{"id": "1", "text": "x", "media": []}]), \
             patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "8967870222"}), \
             patch.object(telegram, "bot_request", side_effect=lambda name, data=None: {
                 "getMe": {"ok": True, "id": 42, "username": "bot_name"},
                 "getChat": {"ok": True, "type": "channel", "title": "Real Channel", "username": "real_channel"},
                 "getChatMember": {"ok": True, "status": "member"},
             }.get(name, {})):
            with self.assertRaisesRegex(RuntimeError, "администратором"):
                worker.validate_configuration()

    def test_polling_processes_new_posts_from_every_source(self):
        config = {
            "sources": ["source_one", "source_two"],
            "destination": "@destination",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
            "enabled": True,
        }
        feeds = {
            source: [{"id": "2", "text": "New post", "media": []}]
            for source in config["sources"]
        }
        sent = []
        cursors = {source: 1 for source in config["sources"]}

        with patch.object(database, "get_config", return_value=config), \
             patch.object(database, "get_cursor", side_effect=lambda source: cursors[source]), \
             patch.object(database, "set_cursor", side_effect=lambda source, value: cursors.update({source: value})), \
             patch.object(database, "already_forwarded", return_value=False), \
             patch.object(database, "write_history"), \
             patch.object(database, "set_state"), \
             patch.object(telegram, "fetch_public_posts", side_effect=lambda source: feeds[source]), \
             patch.object(telegram, "publish_post", side_effect=lambda post, destination, signature="": sent.append(destination) or True), \
             patch.object(telegram, "BOT_TOKEN", "123:test"):
            worker.poll_once()

        self.assertEqual(sent, ["@destination", "@destination"])
        self.assertEqual(cursors, {"source_one": 2, "source_two": 2})

    def test_post_already_forwarded_is_not_sent_again(self):
        config = {
            "sources": ["source_one"],
            "destination": "@destination",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
            "enabled": True,
        }
        feeds = {"source_one": [{"id": "2", "text": "New post", "media": []}]}
        sent = []
        cursors = {"source_one": 1}

        with patch.object(database, "get_config", return_value=config), \
             patch.object(database, "get_cursor", side_effect=lambda source: cursors[source]), \
             patch.object(database, "set_cursor", side_effect=lambda source, value: cursors.update({source: value})), \
             patch.object(database, "already_forwarded", return_value=True), \
             patch.object(database, "write_history"), \
             patch.object(database, "set_state"), \
             patch.object(telegram, "fetch_public_posts", side_effect=lambda source: feeds[source]), \
             patch.object(telegram, "publish_post", side_effect=lambda post, destination, signature="": sent.append(destination) or True), \
             patch.object(telegram, "BOT_TOKEN", "123:test"):
            worker.poll_once()

        self.assertEqual(sent, [])
        self.assertEqual(cursors, {"source_one": 2})


class MenuButtonTests(unittest.TestCase):
    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {"ok": True, "result": True}

    def test_menu_button_is_json_encoded_for_admin_chat(self):
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self.Response()) as send:
            worker.configure_menu_button("https://example.com/", 555)

        data = send.call_args.kwargs["data"]
        self.assertEqual(data.get("chat_id"), 555)
        self.assertIsInstance(data["menu_button"], str)
        menu = json.loads(data["menu_button"])
        self.assertEqual(menu["type"], "web_app")
        self.assertEqual(menu["web_app"]["url"], "https://example.com/")

    def test_menu_button_defaults_when_no_admin_id(self):
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self.Response()) as send:
            worker.configure_menu_button("https://example.com", None)

        calls = send.call_args_list
        self.assertEqual(len(calls), 1)
        data = calls[0].kwargs["data"]
        self.assertNotIn("chat_id", data)
        self.assertIsInstance(data["menu_button"], str)

    def test_menu_button_updates_default_and_admin_chat(self):
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=self.Response()) as send:
            worker.configure_menu_button("https://example.com", 555)

        calls = send.call_args_list
        self.assertEqual(len(calls), 2)
        by_chat = {}
        for call in calls:
            data = call.kwargs["data"]
            by_chat[data.get("chat_id")] = data
        self.assertIn(None, by_chat)   # кнопка по умолчанию
        self.assertIn(555, by_chat)    # кнопка в чате владельца
        menu = json.loads(by_chat[None]["menu_button"])
        self.assertEqual(menu["web_app"]["url"], "https://example.com/")


class MtprotoTests(unittest.TestCase):
    def test_map_entities_covers_main_types(self):
        from telethon.tl import types as tl_types

        from app import mtproto

        entities = mtproto.map_entities([
            tl_types.MessageEntityBold(offset=0, length=4),
            tl_types.MessageEntityTextUrl(offset=5, length=6, url="https://example.org/"),
            tl_types.MessageEntityCustomEmoji(offset=12, length=2, document_id=123),
            tl_types.MessageEntitySpoiler(offset=15, length=3),
        ])
        by_type = {item["type"]: item for item in entities}
        self.assertEqual(by_type["bold"]["offset"], 0)
        self.assertEqual(by_type["text_link"]["url"], "https://example.org/")
        self.assertEqual(by_type["custom_emoji"]["custom_emoji_id"], "123")
        self.assertEqual(by_type["spoiler"]["length"], 3)

    def test_map_buttons_keeps_url_only(self):
        from telethon.tl import types as tl_types

        from app import mtproto

        url_button = tl_types.KeyboardButton(text="Купить", type=tl_types.InlineButtonTypeUrl(url="https://shop.example/item"))
        cb_button = tl_types.KeyboardButton(text="Скрытая", type=tl_types.InlineButtonTypeCallback(data=b"1"))
        markup = tl_types.ReplyInlineMarkup(rows=[tl_types.KeyboardButtonRow(buttons=[url_button, cb_button])])
        result = mtproto.map_buttons(markup)
        self.assertEqual(result, {"inline_keyboard": [[{"text": "Купить", "url": "https://shop.example/item"}]]})
        self.assertIsNone(mtproto.map_buttons(None))

    def test_group_posts_merges_album(self):
        from app import mtproto

        posts = [
            {"id": "10", "text": "Обложка", "entities": [], "media": [{"type": "photo"}], "_mt_grouped": 99},
            {"id": "11", "text": "", "entities": [], "media": [{"type": "photo"}], "_mt_grouped": 99},
            {"id": "12", "text": "Одиночный", "entities": [], "media": [], "_mt_grouped": None},
        ]
        merged = mtproto.group_posts(posts)
        self.assertEqual([p["id"] for p in merged], ["11", "12"])
        album = merged[0]
        self.assertEqual(len(album["media"]), 2)
        self.assertEqual(album["text"], "Обложка")
        self.assertEqual(album["_mt_group_ids"], ["10", "11"])

    def test_split_with_entities_keeps_all_text(self):
        content = "Начало " + ("слово со ссылкой " * 300) + "конец"
        entities = [{"offset": 7, "length": 5, "type": "bold"}]
        parts = telegram._split_with_entities(content, entities, telegram.MAX_MESSAGE_UNITS)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(text for text, _ in parts), content)
        first_text, first_ents = parts[0]
        self.assertEqual(first_ents, entities)

    def test_publish_rich_post_sends_entities_and_keyboard(self):
        post = {
            "text": "Новость дня",
            "entities": [{"offset": 0, "length": 7, "type": "bold"}],
            "reply_markup": {"inline_keyboard": [[{"text": "Читать", "url": "https://example.org/"}]]},
            "media": [{"type": "photo", "filename": "orig.jpg", "data": b"bytes"}],
        }
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=MessageTests()._bot_response()) as send:
            with patch.object(telegram, "download_media", side_effect=AssertionError("байты уже есть")):
                telegram.publish_post(dict(post), "@dest", "")

        data = send.call_args.kwargs["data"]
        self.assertEqual(json.loads(data["entities"] if "entities" in data else data["caption_entities"])[0]["type"], "bold")
        self.assertIn("reply_markup", data)

    def test_publish_poll_calls_send_poll(self):
        post = {
            "text": "",
            "media": [],
            "poll": {"question": "Лучший?", "options": ["Да", "Нет"], "is_anonymous": True},
        }
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=MessageTests()._bot_response()) as send:
            telegram.publish_post(dict(post), "@dest", "")

        method = send.call_args.args[0].rsplit("/", 1)[-1]
        self.assertEqual(method, "sendPoll")

    def test_fill_test_keys_stores_public_credentials(self):
        import tempfile

        from app import database, mtproto

        tmp_dir = tempfile.TemporaryDirectory()
        original_path = database.DATABASE_PATH
        try:
            database.DATABASE_PATH = Path(tmp_dir.name) / "test.sqlite3"
            database.initialize()
            result = mtproto.fill_test_keys()
            self.assertEqual(database.get_state("mt_api_id"), mtproto.TEST_API_ID)
            self.assertEqual(database.get_state("mt_api_hash"), mtproto.TEST_API_HASH)
            self.assertEqual(result["api_id"], mtproto.TEST_API_ID)
            self.assertFalse(mtproto.available())
        finally:
            database.DATABASE_PATH = original_path
            tmp_dir.cleanup()


class DirectTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from app import database

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp_dir.name) / "test.sqlite3"
        database.initialize()
        database.set_config({
            "sources": [], "destination": "@destination", "filter_enabled": False,
            "filter_terms": "", "filter_mode": "any", "enabled": True,
        })
        database.set_state("admin_telegram_id", "777")

    def tearDown(self):
        from app import database

        database.DATABASE_PATH = self.original_path
        self.tmp_dir.cleanup()

    def _updates(self, messages):
        return [
            {"update_id": 100 + index, "message": message}
            for index, message in enumerate(messages)
        ]

    def test_forwarded_dm_is_copied_to_destination(self):
        from app import database, telegram, worker

        message = {
            "message_id": 5, "text": "Смотри пост",
            "chat": {"id": 777, "type": "private"}, "from": {"id": 777},
        }
        calls = []

        def fake_bot(method, data=None, files=None, timeout=30):
            calls.append(method)
            if method == "getUpdates":
                return self._updates([message]) if len(calls) == 1 else []
            return {"message_id": 9}

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()
            worker.drain_direct_messages()

        self.assertEqual(calls.count("copyMessage"), 1)
        history = database.recent_history()
        self.assertEqual(history[0]["source"], "direct")
        self.assertEqual(history[0]["status"], "forwarded")

    def test_stranger_messages_are_ignored(self):
        from app import database, telegram, worker

        message = {
            "message_id": 6, "text": "Спам",
            "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
        }
        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", return_value=self._updates([message])):
            worker.drain_direct_messages()

        self.assertEqual(database.recent_history(), [])
        self.assertEqual(database.get_state("bot_updates_offset"), "101")

    def test_stranger_can_ask_id_but_not_forward(self):
        from app import database, telegram, worker

        who = {
            "message_id": 8, "text": "/id",
            "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
        }
        spam = {
            "message_id": 9, "text": "Спам",
            "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
        }
        sent = []

        def fake_bot(method, data=None, files=None, timeout=30):
            if method == "getUpdates":
                return self._updates([who, spam])
            sent.append(method)
            return {"message_id": 1}

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()

        self.assertIn("sendMessage", sent)
        self.assertNotIn("copyMessage", sent)
        self.assertEqual(database.recent_history(), [])


class BotCommandsTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from app import database

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp_dir.name) / "test.sqlite3"
        database.initialize()
        database.set_config({
            "sources": ["one"], "destination": "@destination", "filter_enabled": False,
            "filter_terms": "", "filter_mode": "any", "enabled": False,
        })
        database.set_state("admin_telegram_id", "777")

    def tearDown(self):
        from app import database

        database.DATABASE_PATH = self.original_path
        self.tmp_dir.cleanup()

    def _updates(self, messages):
        return [
            {"update_id": 200 + index, "message": message}
            for index, message in enumerate(messages)
        ]

    def _updates(self, messages):
        return [
            {"update_id": 200 + index, "message": message}
            for index, message in enumerate(messages)
        ]

    def _drain(self, texts, bot_request):
        from app import telegram, worker

        messages = [
            {"message_id": 10 + index, "text": text,
             "chat": {"id": 777, "type": "private"}, "from": {"id": 777}}
            for index, text in enumerate(texts)
        ]

        def fake_bot(method, data=None, files=None, timeout=30):
            if method == "getUpdates":
                return self._updates(messages)
            return bot_request(method, data)

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()

    def test_add_list_delete_source(self):
        from app import database

        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain(["/add https://t.me/twochan", "/list", "/del 1", "/list"], bot_request)
        self.assertIn("@twochan добавлен", answers[0])
        self.assertIn("@one", answers[1])
        self.assertIn("@twochan", answers[1])
        self.assertIn("@one убран", answers[2])
        self.assertNotIn("@one", answers[3])
        self.assertIn("@twochan", answers[3])
        self.assertEqual(database.get_config()["sources"], ["twochan"])

    def test_add_rejects_garbage_and_duplicates(self):
        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain(["/add not a channel!!", "/add @one"], bot_request)
        self.assertTrue(answers[0])
        self.assertIn("уже в списке", answers[1])

    def test_destination_is_normalized(self):
        from app import database

        def bot_request(method, data):
            return {"message_id": 1}

        self._drain(["/dest https://t.me/somechannel"], bot_request)
        self.assertEqual(database.get_config()["destination"], "@somechannel")

    def test_on_off_and_status(self):
        from app import database

        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain(["/off", "/status", "/on", "/status"], bot_request)
        self.assertIn("остановлена", answers[0].lower())
        self.assertIn("выключена", answers[1].lower())
        self.assertIn("включена", answers[2].lower())
        self.assertTrue(database.get_config()["enabled"])

    def test_start_sends_mini_app_keyboard(self):
        payloads = []

        def bot_request(method, data):
            if method == "sendMessage":
                payloads.append(data)
            return {"message_id": 1}

        with patch.dict("os.environ", {"APP_URL": "https://example.com"}):
            self._drain(["/start"], bot_request)

        self.assertEqual(len(payloads), 1)
        markup = json.loads(payloads[0]["reply_markup"])
        button = markup["keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://example.com/")

    def test_stranger_command_gets_no_answer(self):
        from app import telegram, worker

        message = {
            "message_id": 50, "text": "/status",
            "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
        }
        sent = []

        def fake_bot(method, data=None, files=None, timeout=30):
            if method == "getUpdates":
                return self._updates([message])
            sent.append(method)
            return {"message_id": 1}

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()

        self.assertEqual(sent, [])

    def _drain_texts(self, texts, bot_request):
        from app import telegram, worker

        messages = [
            {"message_id": 60 + index, "text": text,
             "chat": {"id": 777, "type": "private"}, "from": {"id": 777}}
            for index, text in enumerate(texts)
        ]

        def fake_bot(method, data=None, files=None, timeout=30):
            if method == "getUpdates":
                return self._updates(messages)
            return bot_request(method, data)

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()

    def test_menu_buttons_answer_like_mini_app(self):
        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain_texts(["📊 Статус", "📈 Статистика", "❓ Помощь"], bot_request)
        self.assertIn("Пересылка", answers[0])
        self.assertIn("Переслано", answers[1])
        self.assertIn("Команды", answers[2])

    def test_toggle_button_flips_service(self):
        from app import database

        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain_texts(["▶️ Запустить", "⏹ Остановить"], bot_request)
        self.assertIn("включена", answers[0].lower())
        self.assertIn("остановлена", answers[1].lower())
        self.assertFalse(database.get_config()["enabled"])

    def test_pending_add_flow_takes_link(self):
        from app import database

        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain_texts(["➕ Добавить", "https://t.me/newchanname"], bot_request)
        self.assertIn("Пришлите", answers[0])
        self.assertIn("@newchanname добавлен", answers[1])
        self.assertIn("newchanname", database.get_config()["sources"])

    def test_pending_cancelled_by_command(self):
        from app import database

        answers = []

        def bot_request(method, data):
            answers.append(data.get("text", ""))
            return {"message_id": 1}

        self._drain_texts(["➕ Добавить", "/list"], bot_request)
        self.assertIn("Пришлите", answers[0])
        self.assertIn("Источники", answers[1])
        self.assertNotIn("newchanname", database.get_config()["sources"])

    def test_every_answer_carries_main_keyboard(self):
        payloads = []

        def bot_request(method, data):
            if method == "sendMessage":
                payloads.append(data)
            return {"message_id": 1}

        with patch.dict("os.environ", {"APP_URL": "https://example.com"}):
            self._drain_texts(["📊 Статус"], bot_request)

        markup = json.loads(payloads[0]["reply_markup"])
        flat = [button.get("text", "") for row in markup["keyboard"] for button in row]
        self.assertIn("📱 Открыть приложение", flat)
        self.assertIn("📊 Статус", flat)
        app_button = [b for row in markup["keyboard"] for b in row if b.get("web_app")][0]
        self.assertEqual(app_button["web_app"]["url"], "https://example.com/")

    def test_start_command_answers_with_help(self):
        from app import telegram, worker

        message = {
            "message_id": 7, "text": "/start",
            "chat": {"id": 777, "type": "private"}, "from": {"id": 777},
        }
        sent = []

        def fake_bot(method, data=None, files=None, timeout=30):
            if method == "getUpdates":
                return self._updates([message])
            sent.append((method, (data or {}).get("text", "")))
            return {"message_id": 1}

        with patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch.object(telegram, "bot_request", side_effect=fake_bot):
            worker.drain_direct_messages()

        self.assertTrue(any(method == "sendMessage" and "перешлите" in text for method, text in sent))


if __name__ == "__main__":
    unittest.main()
