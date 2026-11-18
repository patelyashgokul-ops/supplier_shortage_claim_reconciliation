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
        cls.physical_reviewer = new_test_user(
            cls.env, login='ssc_replacement_physical_reviewer',
            groups=M + 'group_warehouse_reviewer,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def _validate(self, picking, quantity=None):
        if quantity == 85:
            quantity = getattr(self, 'initial_receipt_qty', 85)
        return super()._validate(picking, quantity)

    def _promised(self, promised_quantity=12, **approval):
        claim, _, sub, _ = self._queued(**approval)
        self.assertEqual(self._send_mocked(sub), 1)
        line = claim.claim_line_ids
        response = self.env['supplier.shortage.claim.response'].with_user(self.maker).create({
            'submission_id': sub.id, 'supplier_contact_id': self.supplier.id,
            'response_type': 'acceptance', 'response_source': 'phone',
            'response_reference': 'Replacement agreement recorded after supplier callback',
            'response_summary': 'Supplier agreed to send goods against this shortage claim.',
            'line_ids': [Command.create({'claim_line_id': line.id, 'accepted_qty': promised_quantity})],
        })
        reviewer = response.with_user(self.sender)
        reviewer.verification_note = 'Checked supplier identity, exact claim revision and replacement commitment.'
        reviewer.action_verify()
        promise = self.env['supplier.shortage.claim.commitment'].with_user(self.sender).create({
            'submission_id': sub.id, 'response_id': response.id, 'commitment_type': 'replacement',
            'claim_line_id': line.id, 'product_uom_id': line.purchase_line_id.uom_id.id,
            'committed_qty': promised_quantity, 'currency_id': sub.currency_id.id, 'promised_date': fields.Date.today(),
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
        row.with_user(self.physical_reviewer).action_warehouse_verify()

    def test_partial_receipts_and_frozen_currency_value(self):
        self.initial_receipt_qty = 80
        claim, promise = self._promised()
        self.assertEqual(claim.verified_recovery_amount, 0)
        supplier_dispatch = promise.with_user(self.sender)
        supplier_dispatch.status_note = 'Supplier reports shipment. Warehouse has yet to receive and validate the goods.'
        supplier_dispatch.action_pending_verification()
        self.assertEqual(claim.verified_recovery_amount, 0)
        approved = claim.verified_amount
        stock = claim.purchase_id
        move_a = additional_receipt(stock, 4).move_ids
        pack = self.env['uom.uom'].create({
            'name': 'SSC replacement pack of three',
            'relative_uom_id': stock.order_line.uom_id.id, 'relative_factor': 3,
        })
        move_b = additional_receipt(stock, 2, source_uom=pack).move_ids
        first = self._draft(claim, promise, move_a, 4)
        second = self._draft(claim, promise, move_b, 1)
        before_moves = (move_a.quantity, move_b.quantity)
        documents = self.env['account.move'].search_count([])
        self.assertEqual(second.remaining_promise_qty, 12)
        for row in first | second:
            self._review(row)
            self.assertEqual(row.effective_qty, 0)
            authorized = row.with_user(self.finance)
            authorized.action_verify()
            authorized.action_verify()
        self.assertEqual(second.source_received_qty, 2)
        self.assertEqual(second.source_uom_id, pack)
        self.assertEqual(second.allocated_qty, 3)
        self.assertEqual(second.promised_qty, 12)
        self.assertEqual(second.remaining_promise_qty, 5)
        self.assertEqual(claim.claim_line_ids.replacement_qty, 7)
        self.assertEqual(claim.claim_line_ids.replacement_remaining_qty, 13)
        self.assertEqual(claim.verified_recovery_amount, stock.currency_id.round(approved * 7 / 20))
        self.assertEqual(claim.verified_amount, approved)
        self.assertEqual(stock.order_line.qty_received, 90)
        self.assertEqual((move_a.quantity, move_b.quantity), before_moves)
        self.assertEqual(self.env['account.move'].search_count([]), documents)
        self.assertEqual(claim.audit_event_ids.filtered(
            lambda event: event.change_summary == 'In-kind recovery verified').mapped('replacement_id'), second | first)

    def test_workflow_guards_duplicate_and_unrelated_order(self):
        claim, promise = self._promised()
        move = additional_receipt(claim.purchase_id, 7).move_ids
        row = self._draft(claim, promise, move, 5)
        spoofed_default = self.env['supplier.shortage.replacement'].with_user(self.maker).with_context(
            default_state='verified', default_verified_goods_value=1000000)
        forged = spoofed_default.create({
            'claim_line_id': claim.claim_line_ids.id, 'stock_move_id': move.id,
            'commitment_id': promise.id, 'source_qty': 1,
            'note': 'Documented proposal is awaiting authorized physical and financial review.',
        })
        self.assertEqual(forged.state, 'draft')
        self.assertEqual(forged.verified_goods_value, 0)
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
        self.assertNotIn('In-kind recovery verified', duplicate.audit_ids.mapped('change_summary'))

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
        self.second.store_by = 'lot'
        picking = self.env['stock.picking'].create({
            'partner_id': self.supplier.id, 'picking_type_id': self.wh.in_type_id.id,
            'location_id': self.vendor_location.id, 'location_dest_id': self.wh.lot_stock_id.id,
            'move_ids': [Command.create({'product_id': self.second.id, 'product_uom_qty': 4,
                'uom_id': self.second.uom_id.id, 'location_id': self.vendor_location.id,
                'location_dest_id': self.wh.lot_stock_id.id})],
        })
        picking.action_confirm()
        source = picking.move_ids
        picking.action_assign()
        source.quantity = 4
        lot = self.env['stock.lot'].create({
            'name': 'SSC-REPLACEMENT-LOT', 'product_id': self.second.id, 'company_id': self.company.id,
        })
        source.move_line_ids.lot_id = lot
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
            'stock_move_line_id': source.move_line_ids.id,
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
        unproven = self.env['supplier.shortage.replacement'].with_user(self.maker).create({
            'claim_line_id': line.id, 'stock_move_id': source.id, 'source_qty': 4,
            'stock_move_line_id': source.move_line_ids.id,
            'substitute_claim_qty': 4, 'note': 'The stock item exists but the terms are awaiting supplier evidence.',
        })
        self._review(unproven)
        with self.assertRaisesRegex(ValidationError, 'verified supplier evidence'), self.env.cr.savepoint():
            unproven.with_user(self.finance).action_verify()
        self.assertEqual(unproven.effective_qty, 0)
        row.with_user(self.finance).action_verify()
        self.assertEqual(row.effective_qty, 3)
        self.assertEqual(row.effective_value, claim.currency_id.round(claim.verified_amount * 3 / 15))
        self.assertEqual(row.source_received_qty, 4)
        self.assertFalse(source.purchase_line_id)
        self.assertEqual(row.stock_move_line_id.lot_id, lot)
        self.env['purchase.order.line'].browse(line.purchase_line_id.id).price_unit += 1
        self.assertTrue(row.source_dirty)
        self.assertEqual(row.effective_qty, 0)
        self.assertEqual(row.allocated_qty, 3)


    def test_one_receipt_split_across_partial_claims(self):
        candidate = self._candidates()
        first, first_promise = self._promised(8, candidates=candidate, quantity=8)
        second, second_promise = self._promised(7, candidates=candidate, quantity=7)
        self.assertNotEqual(first, second)
        move = additional_receipt(first.purchase_id, 10).move_ids
        allocated = self._draft(first, first_promise, move, 6)
        self._review(allocated)
        allocated.with_user(self.finance).action_verify()
        over = self._draft(second, second_promise, move, 5)
        self._review(over)
        with self.assertRaisesRegex(ValidationError, 'insufficient unallocated'), self.env.cr.savepoint():
            over.with_user(self.finance).action_verify()
        valid = self._draft(second, second_promise, move, 4)
        self._review(valid)
        valid.with_user(self.finance).action_verify()
        self.assertEqual(allocated.effective_qty + valid.effective_qty, 10)


    def test_canceled_pending_receipt_releases_provisional_tracking_reference(self):
        order = self._order(10)
        claim = self._claim(order)
        picking = self._incoming(order)
        picking.action_assign()
        move = picking.move_ids
        move.quantity = 2
        line = move.move_line_ids[:1]
        self.assertTrue(line)
        allocation = self.env['supplier.shortage.replacement'].with_user(self.maker).create({
            'claim_line_id': claim.claim_line_ids.id, 'stock_move_id': move.id, 'stock_move_line_id': line.id,
            'source_qty': 2, 'note': 'Pending native operation awaiting receipt and claim verification.',
        })
        allocation.action_request_verification()
        self.assertEqual(allocation.state, 'receipt')
        picking.action_cancel()
        self.assertEqual(move.state, 'cancel')
        allocation.invalidate_recordset(['stock_move_line_id'])
        self.assertFalse(allocation.stock_move_line_id)
        self.assertEqual(allocation.effective_qty, 0)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            allocation.with_user(self.physical_reviewer).action_warehouse_verify()


    def test_returned_serial_cannot_be_reused_until_prior_recovery_is_reversed(self):
        claim, promise = self._promised()
        line = claim.claim_line_ids
        self.second.store_by = 'serial'
        serial = self.env['stock.lot'].create({
            'name': 'SSC-REPLACEMENT-SERIAL', 'product_id': self.second.id, 'company_id': self.company.id,
        })
        evidence = self.env['supplier.shortage.claim.evidence'].with_user(self.maker).create({
            'claim_id': claim.id, 'supplier_response_submission_id': promise.submission_id.id,
            'scope_line_ids': [Command.set(line.ids)], 'evidence_type': 'supplier_confirmation',
            'description': 'Supplier confirmed this serial as one equivalent replacement unit.',
            'filename': 'serial.txt', 'upload_data': base64.b64encode(
                b'The stated serial replaces one original unit, including when resent after an authorized return.'),
            'verification_note': 'Checked supplier identity, serial, exact claim revision and the substitution terms.',
        })
        verify_document(evidence)

        def receipt_and_proposal():
            picking = self.env['stock.picking'].create({
                'partner_id': self.supplier.id, 'picking_type_id': self.wh.in_type_id.id,
                'location_id': self.vendor_location.id, 'location_dest_id': self.wh.lot_stock_id.id,
                'move_ids': [Command.create({'product_id': self.second.id, 'product_uom_qty': 1,
                    'uom_id': self.second.uom_id.id, 'location_id': self.vendor_location.id,
                    'location_dest_id': self.wh.lot_stock_id.id})],
            })
            picking.action_confirm()
            picking.action_assign()
            move = picking.move_ids
            move.quantity = 1
            move.move_line_ids.lot_id = serial
            self._validate(picking)
            row = self.env['supplier.shortage.replacement'].with_user(self.maker).create({
                'claim_line_id': line.id, 'stock_move_id': move.id, 'stock_move_line_id': move.move_line_ids.id,
                'source_qty': 1, 'substitute_claim_qty': 1, 'evidence_id': evidence.id,
                'note': 'Supplier independently confirmed this verified serial against the submitted shortage.',
            })
            self._review(row)
            return row.with_user(self.finance)

        first = receipt_and_proposal()
        first.action_verify()
        returned = first.picking_id._create_return()
        returned.action_assign()
        returned.move_ids.quantity = 1
        returned.move_ids.move_line_ids.lot_id = serial
        self._validate(returned)
        self.assertTrue(first.source_dirty)
        second = receipt_and_proposal()
        with self.assertRaisesRegex(ValidationError, 'serial already has'), self.env.cr.savepoint():
            second.action_verify()
        first.decision_reason = 'The original serial was returned and its prior recovery is being corrected.'
        first.action_reverse()
        second.action_verify()
        self.assertEqual(second.effective_qty, 1)
        self.assertEqual(first.effective_qty, 0)
        self.assertEqual(claim.claim_line_ids.replacement_qty, 1)
