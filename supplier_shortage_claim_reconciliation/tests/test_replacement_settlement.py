import base64

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from .preparation_fixtures import additional_receipt, independent_checker, verify_document
from .test_supplier_submission import M, SupplierSubmissionCase


class TestReplacementSettlement(SupplierSubmissionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.maker = new_test_user(
            cls.env, login='ssc_replacement_maker',
            groups=M + 'group_claim_user,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.warehouse = independent_checker(cls.env, cls.company)

    def _promised(self, quantity=12):
        claim, _, sub, _ = self._queued()
        self.assertEqual(self._send_mocked(sub), 1)
        line = claim.claim_line_ids
        response = self.env['supplier.shortage.claim.response'].with_user(self.maker).create({
            'submission_id': sub.id, 'supplier_contact_id': self.supplier.id,
            'response_type': 'acceptance', 'response_source': 'phone',
            'response_reference': 'Replacement agreement recorded after supplier callback',
            'response_summary': 'Supplier agreed to send goods against this shortage claim.',
            'line_ids': [Command.create({'claim_line_id': line.id, 'accepted_qty': quantity})],
        })
        reviewer = response.with_user(self.sender)
        reviewer.verification_note = 'Checked supplier identity, exact claim revision and replacement commitment.'
        reviewer.action_verify()
        promise = self.env['supplier.shortage.claim.commitment'].with_user(self.sender).create({
            'submission_id': sub.id, 'response_id': response.id, 'commitment_type': 'replacement',
            'claim_line_id': line.id, 'product_uom_id': line.purchase_line_id.uom_id.id,
            'committed_qty': quantity, 'currency_id': sub.currency_id.id, 'promised_date': fields.Date.today(),
            'description': 'Supplier promised to replace units under the verified claim-specific agreement.',
        })
        return claim, promise

    def _draft(self, claim, promise, move, qty):
        return self.env['supplier.shortage.replacement'].with_user(self.maker).create({
            'claim_line_id': claim.claim_line_ids.id, 'stock_move_id': move.id,
            'commitment_id': promise.id, 'source_qty': qty,
            'note': 'Supplier confirmed this warehouse shipment against the submitted shortage claim reference.',
        })

    def _review(self, row):
        row.action_request_verification()
        row.with_user(self.warehouse).action_warehouse_verify()

    def test_partial_receipts_and_frozen_currency_value(self):
        claim, promise = self._promised()
        self.assertEqual(claim.verified_recovery_amount, 0)
        approved = claim.verified_amount
        stock = claim.purchase_id
        move_a = additional_receipt(stock, 4).move_ids
        move_b = additional_receipt(stock, 5).move_ids
        first = self._draft(claim, promise, move_a, 4)
        second = self._draft(claim, promise, move_b, 3)
        before_moves = (move_a.quantity, move_b.quantity)
        documents = self.env['account.move'].search_count([])
        for row in first | second:
            self._review(row)
            self.assertEqual(row.effective_qty, 0)
            authorized = row.with_user(self.finance)
            authorized.action_verify()
            authorized.action_verify()
        self.assertEqual(second.promised_qty, 12)
        self.assertEqual(second.remaining_promise_qty, 5)
        self.assertEqual(claim.claim_line_ids.replacement_qty, 7)
        self.assertEqual(claim.claim_line_ids.replacement_remaining_qty, 8)
        self.assertEqual(claim.verified_recovery_amount, stock.currency_id.round(approved * 7 / 15))
        self.assertEqual(claim.verified_amount, approved)
        self.assertEqual((move_a.quantity, move_b.quantity), before_moves)
        self.assertEqual(self.env['account.move'].search_count([]), documents)
        self.assertEqual(claim.audit_event_ids.filtered(
            lambda event: event.change_summary == 'In-kind recovery verified').mapped('replacement_id'), second | first)

    def test_workflow_guards_duplicate_and_unrelated_order(self):
        claim, promise = self._promised()
        move = additional_receipt(claim.purchase_id, 7).move_ids
        row = self._draft(claim, promise, move, 5)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            row.write({'state': 'verified'})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            row.action_verify()
        wrong_move = self._incoming(self._order(10)).move_ids
        cross_company = self._incoming(self._order(10, warehouse=self.other_wh)).move_ids
        with self.assertRaises((AccessError, ValidationError)), self.env.cr.savepoint():
            self._draft(claim, promise, cross_company, 2)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._draft(claim, promise, wrong_move, 2)
        self._review(row)
        row.with_user(self.finance).action_verify()
        duplicate = self._draft(claim, promise, move, 2)
        self._review(duplicate)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            duplicate.with_user(self.finance).action_verify()
        self.assertFalse(duplicate.verified_at)
        self.assertEqual(duplicate.effective_qty, 0)

    def test_linked_supplier_return_and_corrected_allocation(self):
        claim, promise = self._promised()
        picking = additional_receipt(claim.purchase_id, 6)
        row = self._draft(claim, promise, picking.move_ids, 6)
        self._review(row)
        finance = row.with_user(self.finance)
        finance.action_verify()
        returned = picking._create_return()
        self._validate(returned, 2)
        self.assertTrue(row.source_dirty)
        self.assertEqual(row.effective_qty, 0)
        finance.decision_reason = 'Warehouse validated two units returned against the replacement receipt.'
        finance.action_reverse()
        finance.action_reverse()
        self.assertEqual(row.allocated_qty, 6)
        self.assertTrue(row.verified_at)
        corrected = self._draft(claim, promise, picking.move_ids, 4)
        corrected.supersedes_id = row
        self._review(corrected)
        corrected.with_user(self.finance).action_verify()
        self.assertEqual(claim.claim_line_ids.replacement_qty, 4)
        self.assertEqual(corrected.state, 'verified')


    def test_pending_manual_receipt_and_exceptional_substitute(self):
        claim, promise = self._promised()
        line = claim.claim_line_ids
        picking = self.env['stock.picking'].create({
            'partner_id': self.supplier.id, 'picking_type_id': self.wh.in_type_id.id,
            'location_id': self.vendor_location.id, 'location_dest_id': self.wh.lot_stock_id.id,
            'move_ids': [Command.create({'product_id': self.second.id, 'product_uom_qty': 4,
                'uom_id': self.second.uom_id.id, 'location_id': self.vendor_location.id,
                'location_dest_id': self.wh.lot_stock_id.id})],
        })
        picking.action_confirm()
        source = picking.move_ids
        evidence = self.env['supplier.shortage.claim.evidence'].with_user(self.maker).create({
            'claim_id': claim.id, 'supplier_response_submission_id': promise.submission_id.id,
            'scope_line_ids': [Command.set(line.ids)], 'evidence_type': 'supplier_confirmation',
            'description': 'Supplier agreement for four substitute units against three approved units.',
            'filename': 'substitute.txt', 'upload_data': base64.b64encode(
                b'Supplier confirms four substitute units cover three original units against the submitted claim.'),
            'verification_note': 'Reviewed supplier identity, stock reference, exact scope and equivalent quantity.',
        })
        verify_document(evidence)
        row = self.env['supplier.shortage.replacement'].with_user(self.maker).create({
            'claim_line_id': line.id, 'stock_move_id': source.id, 'source_qty': 4,
            'substitute_claim_qty': 3, 'evidence_id': evidence.id,
            'note': 'Verified supplier agreement equates four substitute units to three original units.',
        })
        row.action_request_verification()
        self.assertEqual(row.state, 'receipt')
        self.assertEqual(row.effective_qty, 0)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            row.with_user(self.warehouse).action_warehouse_verify()
        self._validate(picking)
        row.with_user(self.warehouse).action_warehouse_verify()
        row.with_user(self.finance).action_verify()
        self.assertEqual(row.effective_qty, 3)
        self.assertEqual(row.effective_value, claim.currency_id.round(claim.verified_amount * 3 / 15))
        self.assertEqual(row.source_received_qty, 4)
        self.assertFalse(source.purchase_line_id)
