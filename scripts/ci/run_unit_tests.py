"""Run the Python suite and leave machine-readable test outcome evidence."""
import json
from pathlib import Path
import unittest


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence_path = Path('work/build-badge/unit-tests.json')

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


def main():
    suite = unittest.defaultTestLoader.discover('scripts', pattern='test_*.py')
    runner = unittest.TextTestRunner(resultclass=RecordingResult, verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
