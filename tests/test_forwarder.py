import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app import auth, database, telegram, worker


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
    def test_splits_long_unicode_text_without_losing_characters(self):
        content = "Начало 😀 " + ("длинный текст " * 900) + "Конец 🔚"
        pieces = telegram.split_text(content, telegram.MAX_MESSAGE_UNITS)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), content)
        self.assertTrue(all(telegram.text_units(piece) <= telegram.MAX_MESSAGE_UNITS for piece in pieces))

    def test_long_photo_caption_continues_as_messages(self):
        content = "Начало " + ("подробное описание " * 110) + "Конец"

        class Response:
            ok = True
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}

        with patch.object(telegram, "download_media", return_value=("photo.jpg", b"photo")), \
             patch.object(telegram, "BOT_TOKEN", "123:test"), \
             patch("app.telegram.requests.post", return_value=Response()) as send:
            telegram.publish_post({"text": content, "media": [{"type": "photo", "url": "unused"}]}, "@dest")
            requests = [call.kwargs["data"] for call in send.call_args_list]

        delivered = requests[0]["caption"] + "".join(item["text"] for item in requests[1:])
        self.assertEqual(delivered, content)
        self.assertLessEqual(telegram.text_units(requests[0]["caption"]), telegram.MAX_CAPTION_UNITS)


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


class WorkerTests(unittest.TestCase):
    def test_polling_processes_new_posts_from_every_source(self):
        config = {
            "sources": ["source_one", "source_two"],
            "destination": "@destination",
            "filter_enabled": False,
            "filter_terms": "",
            "filter_mode": "any",
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
             patch.object(database, "write_history"), \
             patch.object(database, "set_state"), \
             patch.object(telegram, "fetch_public_posts", side_effect=lambda source: feeds[source]), \
             patch.object(telegram, "publish_post", side_effect=lambda post, destination: sent.append(destination) or True), \
             patch.object(telegram, "BOT_TOKEN", "123:test"):
            worker.poll_once()

        self.assertEqual(sent, ["@destination", "@destination"])
        self.assertEqual(cursors, {"source_one": 2, "source_two": 2})


if __name__ == "__main__":
    unittest.main()
