# SPDX-License-Identifier: Apache-2.0
"""Parity guards must detect shared errors and allow documented consumption."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'bench/compare/contract_parity.py'
spec = importlib.util.spec_from_file_location('contract_parity', path)
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


def outcome(produced, consumed):
    return {'error': 'limit_exceeded', 'produced': str(produced),
            'consumed': str(consumed), 'members': '1', 'trailing': '0', 'fnv1a': '9'}


class ContractParity(unittest.TestCase):
    def test_output_stop_allows_different_consumption(self):
        failures = []
        parity.compare('output stop', outcome(4, 3), outcome(4, 4), failures, [0], 4)
        self.assertEqual(failures, [])

    def test_shared_output_overrun_is_rejected(self):
        failures = []
        parity.compare('bad outputs', outcome(5, 3), outcome(5, 3), failures, [0], 4)
        self.assertTrue(failures, 'matching wrong outputs must not pass')

    def test_shared_input_overrun_is_rejected(self):
        failures = []
        parity.compare('bad inputs', outcome(4, 11), outcome(4, 11), failures, [0], 4, 10)
        self.assertTrue(failures)

    def test_shared_negative_count_is_rejected(self):
        failures = []
        parity.compare('negative counts', outcome(-1, -1), outcome(-1, -1), failures, [0], 4, 10)
        self.assertTrue(failures)

    def test_input_stop_does_not_require_full_output(self):
        failures = []
        parity.compare('input stop', outcome(2, 10), outcome(2, 10), failures, [0], 4, 10)
        self.assertEqual(failures, [])

    def test_partial_output_stop_still_compares_consumed(self):
        failures = []
        parity.compare('partial stop', outcome(2, 3), outcome(2, 4), failures, [0], 4, 10)
        self.assertTrue(failures)


if __name__ == '__main__':
    unittest.main()
