from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError


BILLING_TYPES = [
    ('unmatched_vendor_bill_line', 'Unmatched Vendor Bill Line'),
    ('posted_billed_above_received', 'Posted Billing Above Receipts'),
    ('draft_billed_above_received', 'Draft Billing Above Receipts'),
    ('potential_duplicate_bill', 'Potential Duplicate Vendor Bill'),
    ('supplier_mismatch', 'Bill Supplier Mismatch'), ('currency_mismatch', 'Bill Currency Review'),
    ('incompatible_bill_uom', 'Incompatible Billing UoM'), ('unexpected_credit_note', 'Credit Purpose / Mapping Review'),
    ('canceled_bill_source_changed', 'Canceled Vendor Document'),
    ('bill_source_reconciliation_conflict', 'Billing Source Requires Revalidation'),
]


class BillingException(models.Model):
    _inherit = 'supplier.shortage.receipt.exception'

    reconciliation_kind = fields.Selection([('receiving', 'Physical Receiving'), ('billing', 'Vendor Billing')],
        required=True, readonly=True, default='receiving', index=True)
    source_bill_line_ids = fields.Many2many('account.move.line', readonly=True, string='Billing Source Lines')
    exception_type = fields.Selection(selection_add=BILLING_TYPES, ondelete={key: 'set other_review' for key, label in BILLING_TYPES})

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        permitted = Domain('id', 'access', 'read') & Domain('move_id', 'access', 'read')
        if self.env['account.move']._access_domain('read').is_false() or self.env['account.move.line']._access_domain('read').is_false():
            return domain & Domain('reconciliation_kind', '=', 'receiving')
        return domain & (Domain('reconciliation_kind', '=', 'receiving') | ~Domain('source_bill_line_ids', 'any!', ~permitted))

    def _is_review_manager(self):
        return super()._is_review_manager() or (bool(self) and all(rec.reconciliation_kind == 'billing' for rec in self)
            and self.env.user.has_group('supplier_shortage_claim_reconciliation.group_finance_manager'))

    def _require_operator(self, manager=False):
        if self and any(rec.reconciliation_kind != 'billing' for rec in self):
            return super()._require_operator(manager=manager)
        allowed = self._is_review_manager() if manager else any(self.env.user.has_group('supplier_shortage_claim_reconciliation.' + role)
            for role in ('group_claim_user', 'group_accounts_reviewer'))
        if not allowed:
            raise AccessError(self.env._('An authorized billing reviewer is required for this action.'))
        self.check_access('write')

    def _validate_assignee(self, user):
        if not self or any(rec.reconciliation_kind != 'billing' for rec in self):
            return super()._validate_assignee(user)
        if user:
            user.check_access('read')
            if user.share or not user.active or any(rec.company_id not in user.company_ids for rec in self) or not any(
                    user.has_group('supplier_shortage_claim_reconciliation.' + role) for role in ('group_claim_user', 'group_accounts_reviewer')):
                raise ValidationError(self.env._('Assign an active purchase/accounts reviewer authorized for this company.'))

    @api.constrains('source_bill_line_ids', 'company_id', 'reconciliation_kind')
    def _check_billing_ownership(self):
        for rec in self:
            if any(line.company_id != rec.company_id for line in rec.source_bill_line_ids):
                raise ValidationError(self.env._('Billing evidence must belong to the exception company.'))
            if rec.reconciliation_kind == 'receiving' and rec.source_bill_line_ids:
                raise ValidationError(self.env._('Billing evidence requires a billing review record.'))

    def action_open_billing_sources(self):
        self.ensure_one()
        self.check_access('read')
        self.source_bill_line_ids.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': self.env._('Vendor Billing Evidence'), 'res_model': 'account.move.line',
                'view_mode': 'list,form', 'domain': [('id', 'in', self.source_bill_line_ids.ids)],
                'views': [(self.env.ref('supplier_shortage_claim_reconciliation.view_bill_evidence_list').id, 'list'),
                          (self.env.ref('supplier_shortage_claim_reconciliation.view_bill_evidence_form').id, 'form')],
                'context': {'create': False, 'edit': False, 'delete': False}}
