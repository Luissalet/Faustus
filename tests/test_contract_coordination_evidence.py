"""Coordination effect receipts must not expire unrelated workspace evidence."""
import json
import unittest
from src import contract_criteria as cc
from src.verification import verify_contract_link


class CoordinationEvidenceTests(unittest.TestCase):
    def book(self):
        criteria, _ = cc.extract('The output must contain the key '+chr(96)+'rows'+chr(96)+'.')
        book = cc.CriteriaBook(criteria, strict=True)
        book.persist = False
        return book

    def read(self, book, cid='read'):
        return book.record_call(cid, 'mcp__example__lookup', '{"id":7}',
                                {'output': '{"rows":[7]}', 'exit_code': 0}, kind='read')

    def post(self, book, tool='coordination_post', kind='effect', ok=True):
        return book.record_call('remote', tool,
                                json.dumps({'thread_id': 23, 'body': 'Checked src/app.py'}),
                                {'output': '{"ok":true,"message_id":24}',
                                 'exit_code': 0 if ok else 1}, kind=kind, ok=ok)

    def test_remote_message_keeps_read_fresh_and_retains_effect_receipt(self):
        book = self.book()
        read = self.read(book)
        post = self.post(book)
        self.assertTrue(post['mutation'])  # Still an external effect, not a fabricated read.
        self.assertEqual(book.last_mutation_seq, -1)
        self.assertEqual(verify_contract_link(book.criteria[0], read,
                         last_mutation_seq=book.last_mutation_seq).state, 'verified')

    def test_remote_effect_does_not_reset_an_actual_earlier_write(self):
        book = self.book()
        before = self.read(book, 'before')
        book.record_call('write', 'write_file', '{"path":"src/app.py"}',
                         {'output': 'saved', 'exit_code': 0}, kind='mutation', paths=['src/app.py'])
        after = self.read(book, 'after')
        self.post(book)
        self.assertEqual(book.last_mutation_seq, 2)
        self.assertEqual(verify_contract_link(book.criteria[0], before,
                         last_mutation_seq=book.last_mutation_seq).state, 'pending')
        self.assertEqual(verify_contract_link(book.criteria[0], after,
                         last_mutation_seq=book.last_mutation_seq).state, 'verified')

    def test_auto_proof_and_attempt_cache_survive_coordination(self):
        book = self.book()
        self.read(book)
        book.criteria[0].update(state='proven', auto=True, evidence_refs=['read'])
        book._auto_tried = {('C01', 'read')}
        self.post(book)
        self.assertEqual(book.criteria[0]['state'], 'proven')
        self.assertEqual(book.criteria[0]['evidence_refs'], ['read'])
        self.assertEqual(book._auto_tried, {('C01', 'read')})

    def test_coordination_is_exempt_only_when_classified_as_an_effect(self):
        book = self.book()
        self.read(book)
        self.post(book, kind='mutation')
        self.assertEqual(book.last_mutation_seq, 2)

    def test_other_effects_keep_their_conservative_freshness_behavior(self):
        for tool in ('python', 'mcp__example__write_file', 'create_document'):
            with self.subTest(tool=tool):
                book = self.book()
                self.read(book)
                self.post(book, tool=tool)
                self.assertEqual(book.last_mutation_seq, 2)

    def test_failed_post_keeps_successful_read_without_fabricating_receipt(self):
        book = self.book()
        self.read(book)
        post = self.post(book, ok=False)
        self.assertFalse(post['mutation'])
        self.assertEqual(book.last_mutation_seq, -1)

    def test_generative_shell_still_expires_old_evidence(self):
        book = self.book()
        self.read(book)
        book.record_call('build', 'bash', 'python build.py',
                         {'output': 'built', 'exit_code': 0}, kind='shell')
        self.assertEqual(book.last_mutation_seq, 2)

    def test_notes_remain_advisory_without_marking_anything_proven(self):
        for render in ('start', 'audit', 'gap'):
            with self.subTest(render=render):
                book = self.book()
                note = {'start': book.start_note, 'audit': lambda: book.audit_note('Check the actual request.'),
                        'gap': book.gap_note}[render]()
                self.assertIn('advisory', note.lower())
                self.assertIn('Do not repeat successful external actions', note)
                self.assertEqual(book.counts()['pending'], 1)
                self.assertEqual(book.counts()['proven'], 0)

    def test_wrong_output_still_cannot_prove_a_requirement(self):
        book = self.book()
        read = self.read(book)
        read['output'] = '{"unrelated":true}'
        self.post(book)
        self.assertEqual(verify_contract_link(book.criteria[0], read,
                         last_mutation_seq=book.last_mutation_seq).state, 'pending')


if __name__ == '__main__':
    unittest.main()
