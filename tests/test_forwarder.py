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
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin("", "4322")
            self.assertEqual(ctx.exception.status_code, 429)
            database.set_state("key_locked_until", "0")
            user = main.require_admin("", "4322")
        self.assertEqual(user["auth_method"], "key")


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


if __name__ == "__main__":
    unittest.main()
