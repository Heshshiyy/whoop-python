"""Tests for the WHOOP CLI argument parsing.

No BLE hardware required — tests the argparse structure only.
"""

from whoop.cli import build_parser


class TestCliParser:
    """Verify that every subcommand parses its arguments correctly."""

    def test_scan_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["scan"])
        assert args.command == "scan"
        assert args.timeout == 5.0

    def test_scan_custom_timeout(self):
        parser = build_parser()
        args = parser.parse_args(["scan", "--timeout", "10.5"])
        assert args.timeout == 10.5

    def test_info(self):
        parser = build_parser()
        args = parser.parse_args(["info", "AA:BB:CC:DD:EE:FF"])
        assert args.command == "info"
        assert args.address == "AA:BB:CC:DD:EE:FF"

    def test_record_basic(self):
        parser = build_parser()
        args = parser.parse_args(["record", "AA:BB:CC:DD:EE:FF"])
        assert args.command == "record"
        assert args.address == "AA:BB:CC:DD:EE:FF"
        assert args.duration == 30.0
        assert args.family == "Whoop4"

    def test_record_full_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "record", "00:11:22:33:44:55",
            "--duration", "60",
            "--database", "/tmp/test.db",
            "--family", "Whoop5",
        ])
        assert args.duration == 60.0
        assert args.database == "/tmp/test.db"
        assert args.family == "Whoop5"

    def test_dashboard_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert args.database is None

    def test_dashboard_custom_db(self):
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--database", "/tmp/whoop.db"])
        assert args.database == "/tmp/whoop.db"

    def test_export(self):
        parser = build_parser()
        args = parser.parse_args(["export", "/tmp/output.csv"])
        assert args.command == "export"
        assert args.path == "/tmp/output.csv"

    def test_export_with_db(self):
        parser = build_parser()
        args = parser.parse_args(["export", "out.csv", "--database", "mydb.sqlite"])
        assert args.path == "out.csv"
        assert args.database == "mydb.sqlite"

    def test_battery(self):
        parser = build_parser()
        args = parser.parse_args(["battery", "FF:EE:DD:CC:BB:AA"])
        assert args.command == "battery"
        assert args.address == "FF:EE:DD:CC:BB:AA"

    def test_no_command_fails(self):
        import pytest
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_invalid_family(self):
        """Record command only accepts Whoop4/Whoop5 as --family choices."""
        import pytest
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["record", "AA:BB", "--family", "Whoop3"])
