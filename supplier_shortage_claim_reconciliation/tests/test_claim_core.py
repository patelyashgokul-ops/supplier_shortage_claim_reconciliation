from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger


class TestClaimCore(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'SSC Core B'})
        cls.supplier = cls.env['res.partner'].create({'name': 'SSC Core Shared Supplier'})
        cls.other_supplier = cls.env['res.partner'].create({'name': 'SSC Core Other Supplier'})
        cls.product = cls.env['product.product'].create({'name': 'SSC Core Product'})
        cls.other_product = cls.env['product.product'].create({'name': 'SSC Core Other Product'})
        cls.order = cls._make_order(cls.company)
        cls.second_order = cls._make_order(cls.company)
        cls.foreign_order = cls._make_order(cls.other_company)
        cls.users = {}
        for role in ('claim_user', 'claim_manager', 'module_admin', 'warehouse_user', 'accounts_reviewer', 'finance_manager', 'unrelated'):
            groups = 'base.group_user' if role == 'unrelated' else 'supplier_shortage_claim_reconciliation.group_' + role + ',purchase.group_purchase_user'
            cls.users[role] = new_test_user(cls.env, login='ssc_core_' + role, groups=groups,
                company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.multi = new_test_user(cls.env, login='ssc_core_multi',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id, cls.other_company.id])])

    @classmethod
    def _make_order(cls, company):
        return cls.env['purchase.order'].with_company(company).create({
            'partner_id': cls.supplier.id, 'company_id': company.id, 'state': 'purchase',
            'order_line': [Command.create({'product_id': cls.product.id, 'product_qty': 10, 'price_unit': 5})]})

    def _claim(self, lines=True, user=None, **values):
        vals = {'partner_id': self.supplier.id, 'purchase_id': self.order.id}
        if lines:
            vals['claim_line_ids'] = [Command.create({'purchase_line_id': self.order.order_line.id, 'proposed_qty': 2})]
        vals.update(values)
        return self.env['supplier.shortage.claim'].with_user(user or self.users['claim_user']).create(vals)

    def test_valid_draft_shared_supplier_and_lines(self):
        claim = self._claim()
        self.assertFalse(claim.partner_id.company_id)
        self.assertEqual(claim.company_id, self.company)
        self.assertEqual(claim.state, 'draft')
        self.assertEqual(claim.claim_date, fields.Date.context_today(claim))
        self.assertEqual(claim.claim_line_ids.product_id, self.product)
        self.assertEqual(claim.claim_line_ids.product_uom_id, self.order.order_line.uom_id)
        self.assertEqual(claim.claim_line_ids.company_id, self.company)
        claim.claim_line_ids.write({'proposed_qty': 3})
        self.assertEqual(claim.claim_line_ids.proposed_qty, 3)

    def test_supplier_mismatch_create_and_write(self):
        with self.assertRaisesRegex(ValidationError, 'Supplier must match'), self.cr.savepoint():
            self._claim(partner_id=self.other_supplier.id)
        claim = self._claim()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.write({'partner_id': self.other_supplier.id})
        self.assertEqual(claim.partner_id, self.supplier)

    def test_cross_company_purchase_with_authorized_multi_user(self):
        with self.assertRaises(UserError), self.cr.savepoint():
            self._claim(user=self.multi, lines=False, purchase_id=self.foreign_order.id, company_id=self.company.id)

    def test_wrong_order_line_and_purchase_change(self):
        claim = self._claim()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.claim_line_ids.write({'purchase_line_id': self.second_order.order_line.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.write({'purchase_id': self.second_order.id})
        self.assertEqual(claim.purchase_id, self.order)
        self.assertEqual(len(claim.claim_line_ids), 1)

    def test_cross_company_line_and_parent(self):
        claim = self._claim(user=self.multi)
        with self.assertRaises(UserError), self.cr.savepoint():
            claim.claim_line_ids.write({'purchase_line_id': self.foreign_order.order_line.id})
        foreign = self._claim(user=self.multi,
            lines=False, company_id=self.other_company.id, purchase_id=self.foreign_order.id, user_id=False)
        with self.assertRaises(UserError), self.cr.savepoint():
            claim.claim_line_ids.write({'claim_id': foreign.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.claim_line_ids.write({'company_id': self.other_company.id})

    def test_product_forgery_and_related_fields(self):
        claim = self._claim()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.claim_line_ids.write({'product_id': self.other_product.id})
        for field, value in (('product_uom_id', self.order.order_line.uom_id.id), ('currency_id', self.company.currency_id.id)):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                claim.claim_line_ids.write({field: value})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._claim(claim_line_ids=[Command.create({'purchase_line_id': self.order.order_line.id,
                'product_id': self.other_product.id, 'proposed_qty': 1})])

    def test_currency_source_and_no_supplier_overwrite(self):
        currency = self.env['res.currency'].with_context(active_test=False).search([('id', '!=', self.company.currency_id.id)], limit=1)
        currency.active = True
        order = self._make_order(self.company)
        order.currency_id = currency
        claim = self._claim(lines=False)
        claim.write({'purchase_id': order.id})
        self.assertEqual(claim.currency_id, currency)
        self.assertEqual(claim.partner_id, self.supplier)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.write({'currency_id': self.company.currency_id.id})
        claim.write({'purchase_id': False})
        self.assertEqual(claim.currency_id, self.company.currency_id)

    def test_proposed_quantity_precision_and_nonfinite(self):
        for quantity in (0, -1, 1e-12, float('nan'), float('inf')):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError), self.cr.savepoint():
                self._claim(claim_line_ids=[Command.create({'purchase_line_id': self.order.order_line.id, 'proposed_qty': quantity})])
        claim = self._claim()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.claim_line_ids.write({'proposed_qty': 0})

    def test_empty_and_incomplete_review_rejected(self):
        empty = self._claim(lines=False)
        with self.assertRaisesRegex(ValidationError, 'at least one'), self.cr.savepoint():
            empty.action_submit_for_review()
        incomplete = self._claim(lines=False, purchase_id=False)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            incomplete.action_submit_for_review()
        incomplete.write({'purchase_id': self.order.id, 'claim_line_ids': [Command.create({'proposed_qty': 1})]})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            incomplete.action_submit_for_review()

    def test_submit_records_review_request_and_preserves_identity(self):
        claim = self._claim()
        name = claim.name
        submit_prepared(claim)
        self.assertEqual(claim.state, 'under_review')
        self.assertEqual(claim.review_requested_by, self.users['claim_user'])
        self.assertTrue(claim.review_requested_at)
        self.assertEqual(claim.name, name)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.action_submit_for_review()

    def test_unauthorized_submit_and_return(self):
        claim = self._claim()
        for role in ('warehouse_user', 'accounts_reviewer', 'finance_manager', 'unrelated'):
            with self.subTest(role=role), self.assertRaises(AccessError), self.cr.savepoint():
                claim.with_user(self.users[role]).action_submit_for_review()
        submit_prepared(claim)
        with self.assertRaises(AccessError), self.cr.savepoint():
            claim.action_return_to_draft()

    def test_authorized_return_preserves_history_and_allows_correction(self):
        claim = self._claim()
        submit_prepared(claim)
        requested = claim.review_requested_at
        manager = claim.with_user(self.users['claim_manager'])
        manager.action_return_to_draft()
        self.assertEqual(manager.state, 'draft')
        self.assertEqual(manager.review_requested_at, requested)
        self.assertEqual(manager.review_requested_by, self.users['claim_user'])
        claim.claim_line_ids.write({'proposed_qty': 4})
        submit_prepared(claim)
        claim.with_user(self.users['claim_manager']).action_return_to_draft()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            manager.action_return_to_draft()
        self.env.flush_all()
        messages = claim.audit_event_ids
        self.assertEqual(len(messages.filtered(lambda message: 'submitted for internal review' in message.change_summary)), 2)
        self.assertEqual(len(messages.filtered(lambda message: 'returned to draft for correction' in message.change_summary)), 2)

    def test_direct_state_and_audit_forgery_rejected(self):
        claim = self._claim()
        for state in ('draft', 'under_review', 'approved', 'closed', 'bogus'):
            with self.subTest(state=state), self.assertRaises(AccessError), self.cr.savepoint():
                claim.with_context(allow_state_change=True).write({'state': state})
        for field, value in (('review_requested_by', self.users['claim_manager'].id), ('review_requested_at', fields.Datetime.now())):
            with self.assertRaises(AccessError), self.cr.savepoint():
                claim.write({field: value})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self._claim(state='under_review')
        with self.assertRaises(AccessError), self.cr.savepoint():
            self._claim(review_requested_by=self.users['claim_manager'].id)
        claim = self.env['supplier.shortage.claim'].with_user(self.users['claim_user']).with_context(
            default_state='under_review', default_review_requested_by=self.users['claim_manager'].id).create({'partner_id': self.supplier.id})
        self.assertEqual(claim.state, 'draft')
        self.assertFalse(claim.review_requested_by)

    def test_review_parent_fields_frozen(self):
        claim = self._claim()
        submit_prepared(claim)
        values = {'company_id': self.company.id, 'partner_id': self.supplier.id, 'purchase_id': self.order.id,
                  'claim_date': claim.claim_date, 'user_id': self.users['claim_user'].id, 'note': 'changed'}
        for field, value in values.items():
            with self.subTest(field=field), self.assertRaises(ValidationError), self.cr.savepoint():
                claim.write({field: value})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.with_user(self.users['claim_manager']).write({'purchase_id': False})

    def test_review_direct_child_create_write_and_reparent_blocked(self):
        claim = self._claim()
        draft = self._claim()
        submit_prepared(claim)
        for values in ({'proposed_qty': 4}, {'purchase_line_id': self.second_order.order_line.id}, {'claim_id': draft.id}, {'note': 'changed'}):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                claim.claim_line_ids.write(values)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env['supplier.shortage.claim.line'].with_user(self.users['claim_user']).create({
                'claim_id': claim.id, 'purchase_line_id': self.order.order_line.id, 'proposed_qty': 1})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            draft.claim_line_ids.write({'claim_id': claim.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.claim_line_ids.with_user(self.users['claim_manager']).write({'proposed_qty': 9})

    def test_review_one2many_commands_blocked(self):
        claim = self._claim()
        draft = self._claim()
        submit_prepared(claim)
        commands = [Command.clear(), Command.unlink(claim.claim_line_ids.id), Command.delete(claim.claim_line_ids.id),
                    Command.update(claim.claim_line_ids.id, {'proposed_qty': 8}), Command.set([]),
                    Command.link(draft.claim_line_ids.id), Command.create({'proposed_qty': 1})]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValidationError), self.cr.savepoint():
                claim.write({'claim_line_ids': [command]})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            draft.write({'claim_line_ids': [Command.link(claim.claim_line_ids.id)]})

    def test_deletion_rejected_in_draft_and_review(self):
        claim = self._claim()
        for reviewed in (False, True):
            if reviewed:
                submit_prepared(claim)
            for user in (self.users['claim_user'], self.users['claim_manager'], self.users['module_admin']):
                for record in (claim, claim.claim_line_ids):
                    with self.assertRaises(AccessError), self.cr.savepoint():
                        record.with_user(user).unlink()

    def test_sequence_failure_uniqueness_and_stability(self):
        claim = self._claim()
        name = claim.name
        claim.write({'note': 'draft correction'})
        self.assertEqual(claim.name, name)
        sequence_model = type(self.env['ir.sequence'])
        with patch.object(sequence_model, 'next_by_code', return_value=False):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self._claim(lines=False)
        with patch.object(sequence_model, 'next_by_code', return_value=name), mute_logger('odoo.sql_db'):
            with self.assertRaises(IntegrityError), self.cr.savepoint():
                self._claim(lines=False)
                self.env.flush_all()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.write({'name': 'SSC/FORGED'})
        other = self._claim(user=self.multi, lines=False, company_id=self.other_company.id,
            purchase_id=self.foreign_order.id, user_id=False)
        self.assertNotEqual(claim.name, other.name)

    def test_company_specific_and_unauthorized_access(self):
        claim = self._claim(user=self.multi, lines=False, company_id=self.other_company.id,
            purchase_id=self.foreign_order.id, user_id=False)
        line = self.env['supplier.shortage.claim.line'].with_user(self.multi).create({
            'claim_id': claim.id, 'purchase_line_id': self.foreign_order.order_line.id, 'proposed_qty': 1})
        claim.with_user(self.multi).with_context(allowed_company_ids=[self.other_company.id]).read(['name'])
        user = self.users['claim_user']
        for record in (claim, line):
            with self.assertRaises(AccessError):
                record.with_user(user).read(['note'])
            self.assertFalse(self.env[record._name].with_user(user).search([('id', '=', record.id)]))
            with self.assertRaises(AccessError), self.cr.savepoint():
                record.with_user(user).write({'note': 'forged'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            claim.with_user(user).action_submit_for_review()
        with self.assertRaises(AccessError), self.cr.savepoint():
            claim.with_user(user).with_context(allowed_company_ids=[self.other_company.id]).read(['name'])

    def test_repeated_claims_for_one_purchase_line_allowed(self):
        first = self._claim()
        second = self._claim()
        self.assertEqual(first.claim_line_ids.purchase_line_id, second.claim_line_ids.purchase_line_id)
        submit_prepared(first)
        submit_prepared(second)

    def test_native_form_purchase_line_onchange(self):
        model = self.env['supplier.shortage.claim'].with_user(self.users['claim_user'])
        with Form(model) as form:
            form.partner_id = self.supplier
            form.purchase_id = self.order
            with form.claim_line_ids.new() as line:
                line.purchase_line_id = self.order.order_line
                line.proposed_qty = 2
        claim = form.record
        self.assertEqual(claim.claim_line_ids.product_id, self.product)
        self.assertEqual(claim.claim_line_ids.product_uom_id, self.order.order_line.uom_id)

    def test_review_audit_survives_tracking_context_flags(self):
        claim = self._claim().with_context(tracking_disable=True, mail_notrack=True)
        submit_prepared(claim)
        claim.with_user(self.users['claim_manager']).action_return_to_draft()
        messages = claim.audit_event_ids
        submitted = messages.filtered(lambda message: 'submitted for internal review' in message.change_summary)
        returned = messages.filtered(lambda message: 'returned to draft for correction' in message.change_summary)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len(returned), 1)
        self.assertEqual(submitted.actor_id, self.users['claim_user'])
        self.assertEqual(returned.actor_id, self.users['claim_manager'])
