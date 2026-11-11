"""Reduction performance, fixed-point, retry, and checkpoint regressions."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from servo_fuzzing.reducer import reduce_delta_lines


class TestReducer(unittest.TestCase):
    def reduce(self, source, predicate, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'input.html'
            output = Path(directory) / 'reduced.html'
            original.write_text(source)
            calls = []

            def run(**args):
                content = args['testcase'].read_text()
                calls.append(content)
                interesting = predicate(content, len(calls))
                return SimpleNamespace(crash_info=SimpleNamespace(message='target') if interesting else None)

            with patch('servo_fuzzing.reducer.run_single_test', side_effect=run):
                reduce_delta_lines('servo', original, target_message='target', output_path=output, **kwargs)
            self.assertEqual(original.read_text(), source)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['input.html', 'reduced.html'])
            return output.read_text(), calls

    def test_large_input_reduces_with_few_launches(self):
        source = ''.join(f'noise {i}\n' for i in range(4190)) + 'trigger\n'
        result, calls = self.reduce(source, lambda text, _: 'trigger' in text)
        self.assertEqual(result, 'trigger\n')
        self.assertLess(len(calls), 50)

    def test_revisits_previously_required_lines(self):
        # Removing B makes A removable; C is always necessary.
        interesting = {'A\nB\nC\n', 'A\nC\n', 'C\n'}
        result, _ = self.reduce('A\nB\nC\n', lambda text, _: text in interesting)
        self.assertEqual(result, 'C\n')

    def test_retries_intermittent_panic(self):
        result, _ = self.reduce('noise\ntrigger\n', lambda text, n: n % 2 == 0 and 'trigger' in text, attempts=2)
        self.assertEqual(result, 'trigger\n')

    def test_empty_candidate(self):
        result, _ = self.reduce('noise\n', lambda text, _: True)
        self.assertEqual(result, '')

    def test_interruption_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'input.html'
            output = Path(directory) / 'output.html'
            original.write_text('A\nB\nC\nD\n')
            with patch('servo_fuzzing.reducer.run_single_test', side_effect=[
                SimpleNamespace(crash_info=SimpleNamespace(message='target')),
                SimpleNamespace(crash_info=SimpleNamespace(message='target')),
                KeyboardInterrupt,
            ]):
                with self.assertRaises(KeyboardInterrupt):
                    reduce_delta_lines('servo', original, output_path=output)
            self.assertEqual(output.read_text(), 'C\nD\n')
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_failed_baseline_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'input.html'
            output = Path(directory) / 'output.html'
            original.write_text('input')
            output.write_text('previous')
            with patch('servo_fuzzing.reducer.run_single_test', return_value=SimpleNamespace(crash_info=None)):
                with self.assertRaisesRegex(ValueError, 'Baseline'):
                    reduce_delta_lines('servo', original, output_path=output)
            self.assertEqual(output.read_text(), 'previous')
