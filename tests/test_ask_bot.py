import unittest

import json

from ask_bot import AskBot, build_prompt, image_block, split_answer, stream_result

ASK = {
    "id": "q1", "url": "https://en.wikipedia.org/wiki/Dinosaur", "question": "whats this?",
    "text": "Research by Baron et al.", "by": {"id": "luis", "name": "Luis"},
    "target": {"text": "Research by Baron et al.", "rect": {"x": 1, "y": 2, "w": 3, "h": 4}},
}


class FakeBridge:
    def __init__(self, asks):
        self.asks = asks
        self.calls = []

    def __call__(self, command, args=None, timeout=None):
        self.calls.append((command, args))
        if command == "ghost_room":
            return {"asks": self.asks}
        if command == "ghost_read":
            return {"title": "Dinosaur - Wikipedia", "content": "Intro. " * 3000 + "Research by Baron et al. changed things. " + "Tail. " * 3000}
        if command == "room_ask_image":
            return {"image": "data:image/jpeg;base64,QUJD"}
        if command == "ghost_tab_list":
            return {"tabs": [{"id": 7, "url": "https://en.wikipedia.org/wiki/Dinosaur#Etymology"}]}
        return {}


class AskBotTests(unittest.TestCase):
    def test_answers_each_question_once_on_its_page(self):
        bridge = FakeBridge([ASK])
        bot = AskBot("claude", lambda ask, page=None: "Baron's 2017 shake-up\nThey moved Ornithischia next to theropods.", call=bridge)
        self.assertEqual(bot.poll(), 1)
        self.assertEqual(bot.poll(), 0)
        suggests = [args for command, args in bridge.calls if command == "ghost_suggest"]
        self.assertEqual(len(suggests), 1)
        reply = suggests[0]
        self.assertEqual((reply["tab_id"], reply["reply_to"], reply["kind"]), (7, "q1", "note"))
        self.assertEqual(reply["title"], "Baron's 2017 shake-up")
        shows = [args for command, args in bridge.calls if command == "ghost_show"]
        self.assertEqual([s["status"] for s in shows], ["working", "done"])
        # Done: back in the corner, no highlight on the area; the answer card marks it.
        self.assertFalse(any(k in shows[-1] for k in ("anchor", "rect")))

    def test_a_failed_model_still_closes_the_question(self):
        bridge = FakeBridge([ASK])

        def broken(_ask, _page=None):
            raise RuntimeError("model unavailable")

        AskBot("claude", broken, call=bridge).poll()
        reply = next(args for command, args in bridge.calls if command == "ghost_suggest")
        self.assertEqual((reply["reply_to"], reply["body"]), ("q1", "model unavailable"))

    def test_questions_for_pages_not_open_here_are_left_alone(self):
        bridge = FakeBridge([{**ASK, "url": "https://example.com/other"}])
        AskBot("claude", lambda ask, page=None: "x", call=bridge).poll()
        self.assertFalse([c for c, _ in bridge.calls if c == "ghost_suggest"])

    def test_model_sees_the_page_around_the_selection(self):
        seen = {}

        def answer(ask, page=None):
            seen["prompt"] = build_prompt(ask, page)
            return "Title\nBody"

        AskBot("claude", answer, call=FakeBridge([ASK])).poll()
        prompt = seen["prompt"]
        self.assertIn("Page title: Dinosaur - Wikipedia", prompt)
        self.assertIn("Research by Baron et al. changed things.", prompt)
        self.assertLess(len(prompt), 9000)

    def test_bot_waits_on_each_shared_page_and_leaves_with_you(self):
        bridge = FakeBridge([])
        bot = AskBot("claude", lambda ask, page=None: "x", call=bridge)
        bot.keep_company(["https://en.wikipedia.org/wiki/Dinosaur", "https://example.com/not-open"])
        shows = [args for command, args in bridge.calls if command == "ghost_show"]
        self.assertEqual([(s["tab_id"], s.get("clear")) for s in shows], [(7, None)])
        bot.keep_company(["https://en.wikipedia.org/wiki/Dinosaur"])
        self.assertEqual(len([c for c, _ in bridge.calls if c == "ghost_show"]), 1)  # already there
        bot.keep_company([])
        last = [args for command, args in bridge.calls if command == "ghost_show"][-1]
        self.assertEqual((last["tab_id"], last["clear"]), (7, True))

    def test_prompt_fences_untrusted_text_and_answer_splits(self):
        prompt = build_prompt(ASK)
        self.assertIn("<<<\nwhats this?\n>>>", prompt)
        self.assertIn("Question from Luis", prompt)
        self.assertEqual(split_answer("**Title**\n\nline one\nline two"), ("Title", "line one line two"))
        self.assertEqual(split_answer(""), ("No answer", ""))

    def test_follow_up_prompt_carries_the_conversation(self):
        follow = {**ASK, "id": "q2", "thread": "q1", "question": "do you like them?",
                  "turns": [{"question": "whats this?", "answer": "Baron's shake-up They moved Ornithischia."}]}
        prompt = build_prompt(follow)
        self.assertIn("This is a follow-up", prompt)
        self.assertIn("Asked: whats this?", prompt)
        self.assertIn("Answered: Baron's shake-up", prompt)
        self.assertIn("Selected text:\n<<<\nResearch by Baron et al.", prompt)
        self.assertLess(prompt.index("follow-up"), prompt.index("Question from"))
        self.assertNotIn("follow-up", build_prompt(ASK))
        self.assertNotIn("follow-up", build_prompt({**ASK, "turns": "not a list"}))

    def test_bot_waits_when_every_open_question_is_already_handled(self):
        bridge = FakeBridge([{**ASK, "url": "https://example.com/not-open-here"}])
        bot = AskBot("claude", lambda ask, page=None: "x", call=bridge)
        naps = []
        bot.sleep = naps.append
        self.assertEqual(bot.poll(), 1)
        self.assertEqual(naps, [])
        self.assertEqual(bot.poll(), 0)
        self.assertEqual(naps, [1.0])  # the bridge answers at once while a question is open
        bridge.asks = []
        bot.poll()
        self.assertEqual(naps, [1.0])  # nothing open: the bridge long-polls, no nap

    def test_prompt_asks_for_the_askers_language_and_lists_links(self):
        self.assertIn("Reply in this language: English", build_prompt(ASK))
        prompt = build_prompt({**ASK, "language": "Español", "links": [{"href": "https://a.example/b", "text": "B"}]})
        self.assertIn("Reply in this language: Español", prompt)
        self.assertIn("- B: https://a.example/b", prompt)


if __name__ == "__main__":
    unittest.main()


class CropTests(unittest.TestCase):
    def test_bot_fetches_the_picture_of_a_cropped_area(self):
        seen = {}

        def answer(ask, page=None):
            seen["ask"] = ask
            return "A chart\nSales went up."

        crop = {**ASK, "id": "q2", "image": True, "target": {"rect": {"x": 1, "y": 2, "w": 3, "h": 4}}}
        bridge = FakeBridge([crop])
        AskBot("claude", answer, call=bridge).poll()
        self.assertEqual(seen["ask"]["image_data"], "data:image/jpeg;base64,QUJD")
        self.assertIn("cropped an area", build_prompt(seen["ask"]))
        show = next(args for command, args in bridge.calls if command == "ghost_show")
        self.assertEqual(show["rect"], crop["target"]["rect"])

    def test_image_block_and_stream_result(self):
        self.assertEqual(image_block("data:image/jpeg;base64,QUJD")["source"]["data"], "QUJD")
        self.assertIsNone(image_block("data:image/png;base64,QUJD"))
        out = "\n".join([json.dumps({"type": "system"}), json.dumps({"type": "result", "result": " Title\nBody "})])
        self.assertEqual(stream_result(out), "Title\nBody")
        with self.assertRaises(RuntimeError):
            stream_result(json.dumps({"type": "result", "is_error": True, "result": "boom"}))


class SessionTests(unittest.TestCase):
    def test_later_questions_see_earlier_ones(self):
        prompts = []

        def answer(ask, page=None, session=None):
            prompts.append(build_prompt(ask, page, session))
            return "Arthur Mensch\nCEO of Mistral."

        bridge = FakeBridge([ASK])
        bot = AskBot("claude", answer, call=bridge)
        bot.poll()
        bridge.asks = [{**ASK, "id": "q2", "question": "how does this connect to the previous one?"}]
        bot.poll()
        self.assertNotIn("Earlier in this session", prompts[0])
        self.assertIn("Earlier in this session", prompts[1])
        self.assertIn("Asked: whats this?", prompts[1])
        self.assertIn("Answered: Arthur Mensch. CEO of Mistral.", prompts[1])
