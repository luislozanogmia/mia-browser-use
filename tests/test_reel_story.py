import json
import unittest

from reel_story import STORY_PROMPT, parse_story, story_prompt, write_story

MOMENTS = [
    {"kind": "page", "when": "Oct 2, 10:00", "title": "Pricing", "url": "https://shop.example/pricing"},
    {"kind": "answer", "when": "Oct 2, 10:02", "title": "Pricing", "url": "https://shop.example/pricing",
     "question": "Why is Pro $30?", "answer": "guide: Seats. Pro adds 5 seats."},
    {"kind": "report", "when": "Oct 2, 10:30", "report": "# Findings\nPro is worth it for teams."},
]


class ReelStoryTests(unittest.TestCase):
    def test_prompt_keeps_the_condensing_rule_and_the_language(self):
        self.assertIn("Condense this information 70% images and 30% text, use px 12 min", STORY_PROMPT)
        prompt = story_prompt(MOMENTS, "Español")
        self.assertIn("in this language: Español", prompt)
        self.assertIn("[2] answer", prompt)
        self.assertIn("Asked: Why is Pro $30?", prompt)

    def test_every_moment_is_placed_once_and_bad_values_are_dropped(self):
        raw = "Here you go:\n" + json.dumps({
            "title": "Pricing, read closely", "cover": 99, "takeaways": ["a", "b", "c", "d"],
            "chapters": [{"heading": "Plans", "moments": [1, 2, 2, "3", 50],
                          "visual": {"type": "stats", "items": [{"value": "$30", "label": "Pro"}, {"value": "5", "label": "seats"}]}}],
            "captions": {"1": "The plans side by side.", "x": "no", "9": "no"},
        })
        story = parse_story(raw, 3)
        self.assertEqual([c["moments"] for c in story["chapters"]], [[1, 2], [3]])
        self.assertEqual(story["chapters"][0]["visual"]["type"], "stats")
        self.assertIsNone(story["cover"])
        self.assertEqual(len(story["takeaways"]), 3)
        self.assertEqual(story["captions"], {"1": "The plans side by side."})

    def test_unknown_visuals_and_nonsense_fall_back_to_a_plain_reel(self):
        story = parse_story(json.dumps({"chapters": [{"heading": "A", "moments": [1],
                                                      "visual": {"type": "html", "items": ["<script>"]}}]}), 2)
        self.assertNotIn("visual", story["chapters"][0])
        self.assertEqual(parse_story("no json at all", 2)["chapters"], [{"heading": "The reel", "intro": "", "moments": [1, 2]}])

    def test_write_story_uses_the_given_model_call(self):
        seen = {}

        def run(prompt):
            seen["prompt"] = prompt
            return json.dumps({"title": "Done", "chapters": [{"heading": "All", "moments": [1, 2, 3]}]})

        story = write_story(MOMENTS, "English", run)
        self.assertEqual(story["title"], "Done")
        self.assertIn("The reel has 3 moments", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
