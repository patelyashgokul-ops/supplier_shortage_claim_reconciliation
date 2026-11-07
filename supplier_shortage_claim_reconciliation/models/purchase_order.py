from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    shortage_claim_ids = fields.One2many('supplier.shortage.claim', 'purchase_id', readonly=True, copy=False)
    shortage_claim_count = fields.Integer(string='Shortage Claims', compute='_compute_shortage_claim_count')

    @api.depends('shortage_claim_ids')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_shortage_claim_count(self):
        self.check_access('read')
        Claim = self.env['supplier.shortage.claim']
        counts = {}
        if Claim.has_access('read') and self.ids:
            counts = {order.id: count for order, count in Claim._read_group(
                [('purchase_id', 'in', self.ids)], ['purchase_id'], ['__count'])}
        for order in self:
            order.shortage_claim_count = counts.get(order.id, 0)

    def _check_shortage_claim_source_access(self):
        self.ensure_one()
        self.check_access('read')
        if self.company_id.id not in self.env.companies.ids:
            raise AccessError(self.env._('This purchase order is outside your activated company scope.'))

    def action_create_shortage_claim(self):
        self._check_shortage_claim_source_access()
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_user'):
            raise AccessError(self.env._('Only an authorized purchase claim user can prepare a shortage claim.'))
        self.env['supplier.shortage.claim'].check_access('create')
        return {'type': 'ir.actions.act_window', 'name': self.env._('Prepare Shortage Claim'),
                'res_model': 'supplier.shortage.claim', 'view_mode': 'form',
                'views': [(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id, 'form')],
                'target': 'current', 'context': {
                    'default_purchase_id': self.id, 'default_partner_id': self.partner_id.id,
                    'default_company_id': self.company_id.id, 'default_user_id': self.env.uid,
                    'allowed_company_ids': self.env.companies.ids}}

    def action_view_shortage_claims(self):
        self._check_shortage_claim_source_access()
        self.env['supplier.shortage.claim'].check_access('read')
        return {'type': 'ir.actions.act_window', 'name': self.env._('Shortage Claims'),
                'res_model': 'supplier.shortage.claim', 'view_mode': 'list,form',
                'domain': [('purchase_id', '=', self.id)], 'context': {
                    'default_purchase_id': self.id, 'default_partner_id': self.partner_id.id,
                    'default_company_id': self.company_id.id, 'allowed_company_ids': self.env.companies.ids}}


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _check_shortage_claim_eligible(self):
        self.check_access('read')
        for line in self:
            if (line.display_type or line.is_downpayment or not line.product_id or not line.uom_id
                    or line.product_id.type != 'consu'):
                raise ValidationError(self.env._('Physical shortage proposals require goods purchase lines. Sections, notes, services, combos and down payments are unsupported.'))
