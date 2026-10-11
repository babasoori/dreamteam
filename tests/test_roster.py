"""Validate the local roster before any session can be launched."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dreamteam.roster import load_roster


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.path = self.project / "dreamteam.toml"
        self.valid = (
            'version = 1\n[players.penny]\nname = "Penny"\n'
            'position = "pg"\nagent = "codex"\ncwd = "."\nargs = []\n'
        )

    def load(self, text):
        self.path.write_text(text)
        return load_roster(self.path)

    def test_project_is_resolved_relative_to_roster(self):
        (self.project / "source").mkdir()
        players = self.load(self.valid.replace('cwd = "."', 'cwd = "source"'))
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].cwd, self.project / "source")
        self.assertEqual(players[0].id, "penny")
        self.assertTrue(players[0].role)

    def test_the_answer_is_a_codex_bench_player(self):
        players = self.load(self.valid + (
            '\n[players.the_answer]\nname = "The Answer"\n'
            'position = "bench"\nagent = "codex"\n'
        ))
        self.assertEqual([(p.id, p.name, p.position) for p in players],
                         [("penny", "Penny", "pg"), ("the_answer", "The Answer", "bench")])

    def test_malformed_rosters_are_rejected_before_launch(self):
        cases = {
            "unsupported version": self.valid.replace("version = 1", "version = 2"),
            "reserved Coach identity": self.valid.replace("[players.penny]", "[players.coach]"),
            "no players": "version = 1\n",
            "unsupported provider": self.valid.replace('agent = "codex"', 'agent = "other"'),
            "wrong args type": self.valid.replace("args = []", 'args = "hello"'),
            "non-string argument": self.valid.replace("args = []", "args = [123]"),
            "missing directory": self.valid.replace('cwd = "."', 'cwd = "missing"'),
            "invalid position": self.valid.replace('position = "pg"', 'position = "coach"'),
            "non-string position": self.valid.replace('position = "pg"', 'position = ["pg"]'),
            "invalid player ID": self.valid.replace("[players.penny]", '[players."Penny!"]'),
            "blank name": self.valid.replace('name = "Penny"', 'name = "  "'),
            "control in name": self.valid.replace('name = "Penny"', 'name = "Pen\\nny"'),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.load(text)

    def test_duplicate_position_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.load(self.valid + (
                '\n[players.air_jordan]\nname = "Air Jordan"\n'
                'position = "pg"\nagent = "codex"\n'
            ))


if __name__ == "__main__":
    unittest.main()
