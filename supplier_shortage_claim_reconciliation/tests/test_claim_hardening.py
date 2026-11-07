from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user


class TestClaimHardening(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other = cls.env['res.company'].create({'name': 'SSC Hardened Company B'})
        cls.supplier = cls.env['res.partner'].create({'name': 'SSC Hardened Shared Supplier'})
        cls.product = cls.env['product.product'].create({'name': 'SSC Hardened Shared Product'})
        cls.po = cls.env['purchase.order'].create({'partner_id': cls.supplier.id, 'state': 'purchase',
            'order_line': [Command.create({'product_id': cls.product.id, 'product_qty': 10, 'price_unit': 10})]})
        cls.foreign_po = cls.env['purchase.order'].with_company(cls.other).create({'partner_id': cls.supplier.id, 'state': 'purchase',
            'order_line': [Command.create({'product_id': cls.product.id, 'product_qty': 10, 'price_unit': 10})]})
        cls.users = {}
        for role in ('claim_user', 'claim_manager', 'warehouse_user', 'accounts_reviewer', 'finance_manager', 'module_admin', 'unrelated'):
            groups = 'base.group_user' if role == 'unrelated' else 'supplier_shortage_claim_reconciliation.group_' + role
            cls.users[role] = new_test_user(cls.env, login='ssc_harden_' + role,
                groups=groups + ',purchase.group_purchase_user,base.group_allow_export',
                company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.multi = new_test_user(cls.env, login='ssc_harden_multi',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,base.group_allow_export',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id, cls.other.id])])
        cls.portal = new_test_user(cls.env, login='ssc_harden_portal', groups='base.group_portal',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.Audit = cls.env['supplier.shortage.claim.audit']

    def _claim(self, lines=True, user=None, **extra):
        vals = {'partner_id': self.supplier.id, 'purchase_id': self.po.id}
        if lines:
            vals['claim_line_ids'] = [Command.create({'purchase_line_id': self.po.order_line.id, 'proposed_qty': 2})]
        vals.update(extra)
        return self.env['supplier.shortage.claim'].with_user(user or self.users['claim_user']).create(vals)

    def _events(self, claim, event_type=None):
        events = claim.audit_event_ids
        return events.filtered(lambda event: event.event_type == event_type) if event_type else events

    def test_creation_audit_trusted_actor_and_company(self):
        context = dict(default_create_uid=self.users['claim_manager'].id, default_write_uid=self.users['claim_manager'].id,
            default_create_date='2000-01-01', default_write_date='2000-01-01',
            default_audit_event_ids=[Command.create({'event_type': 'submitted', 'change_summary': 'Forged'})])
        claim = self.env['supplier.shortage.claim'].with_user(self.users['claim_user']).with_context(**context).create({
            'partner_id': self.supplier.id, 'purchase_id': self.po.id})
        self.assertEqual(claim.create_uid, self.users['claim_user'])
        self.assertNotEqual(claim.create_date.year, 2000)
        line = self.env['supplier.shortage.claim.line'].with_user(self.users['claim_user']).with_context(**context).create({
            'claim_id': claim.id, 'purchase_line_id': self.po.order_line.id, 'proposed_qty': 1})
        self.assertEqual(line.create_uid, self.users['claim_user'])
        self.assertNotEqual(line.create_date.year, 2000)
        created = self._events(claim, 'created')
        self.assertEqual(len(created), 1)
        self.assertEqual(created.actor_id, self.users['claim_user'])
        self.assertEqual(created.company_id, claim.company_id)
        self.assertEqual(created.new_state, 'draft')
        self.assertTrue(created.event_datetime)
        self.assertFalse(created.old_state)

    def test_submit_return_events_and_no_repeated_transition(self):
        claim = self._claim().with_context(tracking_disable=True, mail_notrack=True, audit_actor_id=self.users['module_admin'].id)
        submit_prepared(claim)
        submitted = self._events(claim, 'submitted')
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted.old_state, 'draft')
        self.assertEqual(submitted.new_state, 'under_review')
        self.assertEqual(submitted.actor_id, self.users['claim_user'])
        self.assertIn(str(claim.claim_line_ids.id), submitted.valuation_basis)
        self.assertTrue(submitted.details)
        with self.assertRaises(ValidationError):
            claim.action_submit_for_review()
        self.assertEqual(len(self._events(claim, 'submitted')), 1)
        claim.with_user(self.users['claim_manager']).action_return_to_draft()
        returned = self._events(claim, 'returned')
        self.assertEqual(returned.actor_id, self.users['claim_manager'])
        self.assertEqual(returned.old_state, 'under_review')
        self.assertEqual(returned.new_state, 'draft')
        self.assertTrue(returned.reason)

    def test_readonly_roles_and_configuration_role_do_not_acquire_business_authority(self):
        claim = self._claim()
        for role in ('warehouse_user', 'accounts_reviewer', 'finance_manager', 'module_admin', 'unrelated'):
            user = self.users[role]
            with self.subTest(role=role), self.assertRaises(AccessError):
                claim.with_user(user).action_submit_for_review()
        admin = self.users['module_admin']
        self.assertFalse(admin.has_group('supplier_shortage_claim_reconciliation.group_claim_manager'))
        self.assertFalse(admin.has_group('supplier_shortage_claim_reconciliation.group_claim_user'))
        admin.group_ids = [Command.link(self.env.ref('supplier_shortage_claim_reconciliation.group_finance_manager').id),
                           Command.link(self.env.ref('supplier_shortage_claim_reconciliation.group_warehouse_user').id)]
        with self.assertRaises(AccessError):
            self._claim(user=admin)
        submit_prepared(claim)
        for role in ('claim_user', 'warehouse_user', 'accounts_reviewer', 'finance_manager', 'module_admin'):
            with self.assertRaises(AccessError):
                claim.with_user(self.users[role]).action_return_to_draft()

    def test_direct_state_metadata_audit_and_financial_forgery(self):
        claim = self._claim()
        for vals in ({'state': 'under_review'}, {'state': 'approved'}, {'review_requested_by': self.users['claim_manager'].id},
                     {'create_uid': self.users['module_admin'].id}, {'audit_event_ids': [Command.create({'event_type': 'submitted'})]}):
            with self.assertRaises(AccessError):
                claim.with_context(allow_state_change=True, skip_audit=True).write(vals)
        with self.assertRaises(ValidationError):
            claim.claim_line_ids.write({'provisional_untaxed_amount': 999})
        self.assertEqual(claim.state, 'draft')
        self.assertFalse(self._events(claim, 'submitted'))

    def test_batch_transition_validates_all_before_mutating(self):
        valid = self._claim()
        invalid = self._claim(lines=False)
        count = len(self._events(valid))
        with self.assertRaises(ValidationError):
            (valid | invalid).action_submit_for_review()
        self.assertEqual(valid.state, 'draft')
        self.assertEqual(invalid.state, 'draft')
        self.assertFalse(valid.claim_line_ids.valuation_snapshot)
        self.assertEqual(len(self._events(valid)), count)

    def test_mid_transition_failure_rolls_back_snapshots_states_and_audit(self):
        first, second = self._claim(), self._claim()
        line_class = type(first.claim_line_ids)
        original = line_class._capture_valuation_snapshot
        def fail_after_capture(lines, captured_at):
            original(lines, captured_at)
            raise ValidationError('Simulated later failure')
        before = self.Audit.search_count([])
        with patch.object(line_class, '_capture_valuation_snapshot', fail_after_capture):
            with self.assertRaises(ValidationError):
                (first | second).action_submit_for_review()
        self.assertEqual((first | second).mapped('state'), ['draft', 'draft'])
        self.assertFalse(any((first | second).claim_line_ids.mapped('valuation_snapshot')))
        self.assertEqual(self.Audit.search_count([]), before)

    def test_batch_events_are_associated_with_correct_claims(self):
        claims = self._claim() | self._claim()
        submit_prepared(claims)
        for claim in claims:
            event = self._events(claim, 'submitted')
            self.assertEqual(len(event), 1)
            self.assertEqual(event.company_id, claim.company_id)
            self.assertEqual(set(event.valuation_basis), {str(claim.claim_line_ids.id)})

    def test_material_edits_are_logged_but_notes_and_noop_are_not(self):
        claim = self._claim()
        count = len(self._events(claim))
        claim.write({'note': 'Working context', 'purchase_id': self.po.id})
        self.assertEqual(len(self._events(claim)), count)
        claim.claim_line_ids.write({'proposed_qty': 3})
        event = self._events(claim, 'quantity_changed')
        self.assertEqual(len(event), 1)
        self.assertIn('2.0 -> 3.0', event.change_summary)
        self.assertEqual(event.actor_id, self.users['claim_user'])
        replacement = self.env['purchase.order'].create({'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 10, 'price_unit': 12})]})
        claim.write({'purchase_id': replacement.id, 'claim_line_ids': [Command.update(claim.claim_line_ids.id, {'purchase_line_id': replacement.order_line.id})]})
        self.assertTrue(self._events(claim, 'fields_changed'))

    def test_invalid_line_quantity_rolls_back_without_caller_savepoint(self):
        claim = self._claim()
        count = len(self._events(claim))
        with self.assertRaises(ValidationError):
            claim.claim_line_ids.write({'proposed_qty': 0})
        self.assertEqual(claim.claim_line_ids.proposed_qty, 2)
        self.assertEqual(len(self._events(claim)), count)

    def test_outer_rollback_discards_success_event(self):
        claim = self._claim()
        prepare_review(claim)
        count = len(self._events(claim))
        with self.assertRaises(RuntimeError):
            with self.cr.savepoint():
                claim.action_submit_for_review()
                self.assertTrue(self._events(claim, 'submitted'))
                raise RuntimeError('Rollback requested by caller')
        self.assertEqual(claim.state, 'draft')
        self.assertEqual(len(self._events(claim)), count)

    def test_audit_history_cannot_be_created_edited_deleted_or_copied(self):
        claim = self._claim()
        event = self._events(claim, 'created')
        for user in self.users.values():
            for vals in ({'actor_id': user.id}, {'event_datetime': '2000-01-01'}, {'change_summary': 'Forged'}, {'company_id': self.other.id}):
                with self.assertRaises(AccessError):
                    event.with_user(user).write(vals)
            with self.assertRaises(AccessError):
                event.with_user(user).unlink()
            with self.assertRaises(AccessError):
                self.Audit.with_user(user).with_context(skip_audit=True).create({'claim_id': claim.id,
                    'company_id': self.company.id, 'actor_id': user.id, 'event_type': 'submitted',
                    'event_datetime': '2000-01-01', 'change_summary': 'Forged'})
        with self.assertRaises(AccessError):
            event.with_user(self.users['claim_user']).copy()

    def test_company_and_export_isolation_including_audit(self):
        foreign = self._claim(user=self.multi, company_id=self.other.id, purchase_id=self.foreign_po.id,
            user_id=False, claim_line_ids=[Command.create({'purchase_line_id': self.foreign_po.order_line.id, 'proposed_qty': 1})])
        user = self.users['claim_user']
        for record in (foreign, foreign.claim_line_ids, self._events(foreign, 'created')):
            restricted = record.with_user(user)
            with self.assertRaises(AccessError):
                restricted.read(['id'])
            with self.assertRaises(AccessError):
                restricted.export_data(['id'])
            self.assertFalse(self.env[record._name].with_user(user).search([('id', '=', record.id)]))
        for context in ([self.company.id], [self.other.id]):
            current = foreign.with_user(self.multi).with_context(allowed_company_ids=context)
            if self.other.id in context:
                self.assertTrue(current.read(['name']))
                self.assertTrue(current.audit_event_ids)
            else:
                with self.assertRaises(AccessError):
                    current.read(['name'])
        with self.assertRaises(AccessError):
            foreign.with_user(user).write({'note': 'forged'})

    def test_external_and_unrelated_users_cannot_access_claim_line_or_audit(self):
        claim = self._claim()
        for user in (self.portal, self.env.ref('base.public_user'), self.users['unrelated']):
            for record in (claim, claim.claim_line_ids, self._events(claim, 'created')):
                with self.assertRaises(AccessError):
                    record.with_user(user).read(['id'])
                with self.assertRaises(AccessError):
                    self.env[record._name].with_user(user).search([])

    def test_review_children_and_one2many_batch_bypasses_blocked(self):
        draft, review = self._claim(), self._claim()
        submit_prepared(review)
        with self.assertRaises(ValidationError):
            (draft.claim_line_ids | review.claim_line_ids).write({'proposed_qty': 8})
        self.assertEqual(draft.claim_line_ids.proposed_qty, 2)
        with self.assertRaises(ValidationError):
            draft.write({'claim_line_ids': [Command.link(review.claim_line_ids.id)]})
        with self.assertRaises(AccessError):
            review.claim_line_ids.unlink()
        with self.assertRaises(AccessError):
            review.unlink()

    def test_copy_creates_new_draft_reference_and_fresh_history(self):
        claim = self._claim()
        submit_prepared(claim)
        copied = claim.copy()
        self.assertNotEqual(copied.name, claim.name)
        self.assertEqual(copied.state, 'draft')
        self.assertFalse(copied.review_requested_at)
        self.assertFalse(copied.review_currency_id)
        self.assertFalse(copied.claim_line_ids.valuation_snapshot)
        self.assertTrue(self._events(copied, 'created'))
        self.assertFalse(self._events(copied, 'submitted'))
        self.assertFalse(set(copied.audit_event_ids.ids) & set(claim.audit_event_ids.ids))
        with self.assertRaises(AccessError):
            claim.copy({'state': 'under_review'})

    def test_company_identity_is_immutable_and_company_sequence_uses_target(self):
        claim = self._claim(user=self.multi)
        with self.assertRaises(ValidationError):
            claim.write({'company_id': self.other.id, 'purchase_id': False, 'user_id': False})
        self.env['ir.sequence'].create({'name': 'SSC Hardened Company B sequence', 'code': 'supplier.shortage.claim',
            'company_id': self.other.id, 'prefix': 'SSC-B/', 'padding': 6})
        foreign = self._claim(user=self.multi, lines=False, company_id=self.other.id, purchase_id=self.foreign_po.id, user_id=False)
        self.assertTrue(foreign.name.startswith('SSC-B/'))
        claims = self.env[claim._name].with_user(self.users['claim_user']).create([
            {'partner_id': self.supplier.id}, {'partner_id': self.supplier.id}])
        self.assertEqual(len(set(claims.mapped('name'))), 2)

    def test_attachment_company_public_token_and_review_controls(self):
        claim = self._claim()
        Attachment = self.env['ir.attachment'].with_user(self.users['claim_user'])
        values = {'name': 'Claim evidence.txt', 'res_model': claim._name, 'res_id': claim.id, 'raw': b'Private claim context', 'mimetype': 'text/plain'}
        attachment = Attachment.create(values)
        for extra in ({'public': True}, {'access_token': 'forged'}, {'company_id': self.other.id}):
            with self.assertRaises(UserError):
                Attachment.create(dict(values, **extra))
        with self.assertRaises(AccessError):
            attachment.generate_access_token()
        submit_prepared(claim)
        with self.assertRaises(ValidationError):
            attachment.write({'name': 'Changed'})
        with self.assertRaises(ValidationError):
            attachment.unlink()
        attachment.with_user(self.users['claim_user'])._can_return_content()
        # Exercise native scoped-token elevation rather than simulating it with sudo.
        scoped = attachment.with_user(self.env.user)._get_raw_access_token()
        binary = self.env['ir.binary'].with_user(self.portal)
        token_record = binary._find_record(res_model='ir.attachment', res_id=attachment.id, access_token=scoped, field='raw')
        self.assertTrue(token_record.env.su)
        with self.assertRaises(AccessError):
            binary._get_stream_from(token_record)
        with self.assertRaises(AccessError):
            attachment.with_user(self.portal)._can_return_content(access_token='forged')

    def test_cross_company_attachment_search_read_and_creation_are_blocked(self):
        foreign = self._claim(user=self.multi, lines=False, company_id=self.other.id, purchase_id=self.foreign_po.id, user_id=False)
        attachment = self.env['ir.attachment'].with_user(self.multi).create({'name': 'Company B.txt',
            'res_model': foreign._name, 'res_id': foreign.id, 'mimetype': 'text/plain', 'raw': b'Company B confidential'})
        restricted = attachment.with_user(self.users['claim_user'])
        with self.assertRaises(AccessError):
            restricted.export_data(['id'])
        with self.assertRaises(AccessError):
            restricted.read(['name'])
        self.assertFalse(self.env['ir.attachment'].with_user(self.users['claim_user']).search([('id', '=', attachment.id)]))
        with self.assertRaises(AccessError):
            restricted._can_return_content()
        with self.assertRaises(AccessError):
            self.env['ir.attachment'].with_user(self.users['claim_user']).create({'name': 'Forged B', 'res_model': foreign._name, 'res_id': foreign.id})

    def test_audit_form_and_existing_menu_load(self):
        claim = self._claim()
        with Form(claim) as form:
            self.assertEqual(form.partner_id, self.supplier)
        self.assertIn('audit_event_ids', claim.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id, 'form')['arch'])
        self.assertEqual(self.env.ref('supplier_shortage_claim_reconciliation.action_claim').res_model, claim._name)

    def test_same_label_relationship_change_is_still_audited(self):
        claim = self._claim()
        original = self.users['claim_user']
        replacement = self.users['claim_manager']
        replacement.name = original.name
        self.assertEqual(original.display_name, replacement.display_name)
        claim.write({'user_id': replacement.id})
        event = self._events(claim, 'fields_changed')
        self.assertEqual(len(event), 1)
        self.assertIn('record %s' % original.id, event.change_summary)
        self.assertIn('record %s' % replacement.id, event.change_summary)
        claim.action_refresh_provisional_valuation()
        refreshed = self._events(claim, 'valuation_refreshed')
        self.assertEqual(refreshed.valuation_basis[str(claim.claim_line_ids.id)]['currency_id'], claim.currency_id.id)
