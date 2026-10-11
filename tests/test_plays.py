"""Coaching state is durable and independent of player activity."""
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dreamteam.plays import PlayError, PlayStore, brief_text


class PlayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "fastbreak"
        self.project.mkdir()
        self.store = PlayStore(self.root)

    def create(self, **overrides):
        fields = dict(
            project="fastbreak", goal="Build a TWS API CLI foundation.",
            constraints="No external connections.", acceptance="Doctor works offline.",
        )
        fields.update(overrides)
        return self.store.create(**fields)

    def test_new_store_is_empty_and_reading_does_not_create_state(self):
        self.assertIsNone(self.store.load())
        self.assertFalse((self.root / ".dreamteam").exists())

    def test_create_and_reopen_resolves_project_and_keeps_ball_unassigned(self):
        play = self.create(goal="  Build a TWS API CLI foundation.  ")
        self.assertEqual(play.project, self.project)
        self.assertEqual(play.id, "001")
        self.assertEqual(play.status, "planned")
        self.assertIsNone(play.ball_holder)
        self.assertEqual(play.goal, "Build a TWS API CLI foundation.")
        self.assertEqual(PlayStore(self.root).load(), play)
        self.assertEqual(self.store.brief_path(play).read_text(), brief_text(play))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_brief_is_plan_only_and_contains_the_saved_assignment(self):
        play = self.create(constraints="")
        text = brief_text(play)
        for expected in (
            "Penny", "point guard (PG)", str(self.project), play.goal, play.acceptance,
            "No additional constraints specified.", "plan only", "Do not edit files",
            "connect to external services", "coach's explicit go-ahead",
            "does not send it to a player or assign possession",
            "Acknowledge receipt, then wait for the coach's explicit go-ahead before inspecting or planning.",
            "Implementation is outside this assignment",
            "requires a separate assignment from the coach",
        ):
            self.assertIn(expected, text)

    def test_invalid_inputs_do_not_create_any_state(self):
        cases = (
            {"project": ""}, {"project": "missing"}, {"project": 1}, {"project": "bad\0path"},
            {"goal": ""}, {"goal": "  "}, {"goal": None},
            {"acceptance": ""}, {"acceptance": []}, {"constraints": None},
            {"goal": "hidden\x1b[31m"}, {"constraints": "embedded\0null"},
            {"acceptance": "bad\x7ftext"}, {"goal": "bad\x9btext"},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(PlayError):
                    self.create(**values)
                self.assertFalse((self.root / ".dreamteam").exists())
        file_path = self.root / "file.txt"
        file_path.write_text("not a directory")
        with self.assertRaises(PlayError):
            self.create(project=file_path)

    def test_multiline_text_and_tabs_are_preserved(self):
        play = self.create(goal="First line\nSecond line", constraints="One\tTwo")
        self.assertEqual(self.store.load(), play)
        self.assertIn("First line\nSecond line", brief_text(play))

    def test_existing_play_is_never_overwritten(self):
        original = self.create()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(PlayError, "already exists"):
            self.create(goal="A different assignment")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load(), original)

    def test_invalid_saved_state_is_rejected_and_left_untouched(self):
        self.create()
        valid = json.loads(self.store.path.read_text())
        cases = [
            "", "[]",
            json.dumps(valid).replace('"version": 1,', '"version": 1, "version": 1,'),
            json.dumps({**valid, "version": True}),
            json.dumps({**valid, "extra": "unexpected"}),
            json.dumps({**valid, "project": "relative/path"}),
            json.dumps({**valid, "goal": ""}),
            json.dumps({**valid, "goal": "hidden\x1b[31m"}),
            json.dumps({**valid, "constraints": []}),
            json.dumps({**valid, "acceptance": None}),
            json.dumps({**valid, "created_at": "yesterday"}),
            json.dumps({**valid, "created_at": "2026-10-11T12:00:00"}),
            json.dumps({**valid, "ball_holder": "penny"}),
            json.dumps({**valid, "status": "accepted"}),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                self.store.path.write_text(invalid)
                with self.assertRaisesRegex(PlayError, "Cannot read saved play"):
                    self.store.load()
                with self.assertRaises(PlayError):
                    self.create()
                self.assertEqual(self.store.path.read_text(), invalid)

    def test_incomplete_existing_directory_is_not_replaced(self):
        self.store.play_directory.mkdir(parents=True)
        with self.assertRaisesRegex(PlayError, "Cannot read saved play"):
            self.create()
        self.assertEqual(list(self.store.play_directory.iterdir()), [])

    def test_state_is_authoritative_even_when_brief_is_edited(self):
        play = self.create()
        self.store.brief_path(play).write_text("This is not the saved assignment.")
        self.assertEqual(self.store.load(), play)
        self.assertIn(play.goal, brief_text(self.store.load()))

    def test_overlapping_creation_is_rejected_then_lock_is_reusable(self):
        self.store.directory.mkdir(parents=True)
        with (self.store.directory / ".create.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(PlayError, "Another play"):
                self.create()
            self.assertIsNone(self.store.load())
        self.assertEqual(self.create(), self.store.load())

    def test_failed_commit_does_not_leave_a_partial_play(self):
        with patch("dreamteam.plays.Path.rename", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(PlayError, "Could not save"):
                self.create()
        self.assertIsNone(self.store.load())
        self.assertEqual([p.name for p in self.store.directory.iterdir()], [".create.lock"])
        self.assertEqual(self.create(), self.store.load())


if __name__ == "__main__":
    unittest.main()
