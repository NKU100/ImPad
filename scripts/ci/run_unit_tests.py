"""Run the Python suite and leave machine-readable test outcome evidence."""
import argparse
import json
from pathlib import Path
import sys
import unittest


class RecordingResult(unittest.TextTestResult):
    evidence_path = Path('work/build-badge/unit-tests.json')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def stopTestRun(self):
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(json.dumps({
            'testsRun': self.testsRun,
            'failures': len(self.failures),
            'errors': len(self.errors),
            'unexpectedSuccesses': len(self.unexpectedSuccesses),
            'shouldStop': self.shouldStop,
            'wasSuccessful': self.wasSuccessful(),
        }, separators=(',', ':')) + '\n')
        super().stopTestRun()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--tests', nargs='+', help='Run only the named unittest modules or tests')
    parser.add_argument('--output', type=Path, default=Path('work/build-badge/unit-tests.json'))
    args = parser.parse_args([] if argv is None else argv)
    RecordingResult.evidence_path = args.output
    if args.tests:
        suite = unittest.TestSuite()
        suite.addTests(unittest.defaultTestLoader.loadTestsFromNames(args.tests))
    else:
        suite = unittest.defaultTestLoader.discover('scripts', pattern='test_*.py')
    runner = unittest.TextTestRunner(resultclass=RecordingResult, verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
