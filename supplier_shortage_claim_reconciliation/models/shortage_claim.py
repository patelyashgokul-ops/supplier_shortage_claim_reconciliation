from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ShortageClaim(models.Model):
    _name = 'supplier.shortage.claim'
    _description = 'Supplier Shortage Claim'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'id desc'

    name = fields.Char(required=True, readonly=True, copy=False, default='New', index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    claim_date = fields.Date(string='Claim Date', default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner', string='Supplier', required=True, check_company=True, tracking=True)
    purchase_id = fields.Many2one('purchase.order', string='Purchase Order', check_company=True, ondelete='restrict', tracking=True, index=True)
    user_id = fields.Many2one('res.users', string='Responsible', default=lambda self: self.env.user, check_company=True)
    state = fields.Selection([('draft', 'Draft'), ('under_review', 'Under Review')],
                             required=True, default='draft', readonly=True, tracking=True, copy=False)
    review_currency_id = fields.Many2one('res.currency', string='Submitted Currency', readonly=True, copy=False, ondelete='restrict')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id', readonly=True)
    review_requested_by = fields.Many2one('res.users', string='Review Requested By', readonly=True, copy=False, tracking=True)
    review_requested_at = fields.Datetime(string='Review Requested At', readonly=True, copy=False, tracking=True)
    note = fields.Text(string='Notes')
    claim_line_ids = fields.One2many('supplier.shortage.claim.line', 'claim_id', string='Claim Lines', copy=True, check_company=True)

    audit_event_ids = fields.One2many('supplier.shortage.claim.audit', 'claim_id', string='Audit History', readonly=True, copy=False)

    product_ids = fields.Many2many('product.product', compute='_compute_product_ids', search='_search_product_ids', string='Products')
    purchase_date = fields.Datetime(string='PO Date', compute='_compute_purchase_context')
    purchase_buyer_id = fields.Many2one('res.users', string='Purchase Buyer', compute='_compute_purchase_context')
    purchase_state = fields.Selection([('draft', 'RFQ'), ('sent', 'RFQ Sent'), ('to approve', 'To Approve'),
                                      ('purchase', 'Purchase Order'), ('cancel', 'Cancelled')], compute='_compute_purchase_context', string='PO Status')
    purchase_currency_id = fields.Many2one('res.currency', string='Current PO Currency', compute='_compute_purchase_context')
    purchase_source_status = fields.Selection([('current', 'Current Source'), ('changed', 'Source Requires Review'),
                                              ('unavailable', 'Source Unavailable'), ('unlinked', 'No Purchase Order')], compute='_compute_purchase_context')
    purchase_source_warning = fields.Char(compute='_compute_purchase_context', string='Purchase Source Information')
    review_purchase_context = fields.Json(readonly=True, copy=False)

    _name_unique = models.Constraint('UNIQUE(name)', 'Claim references must be unique.')

    @api.depends('purchase_id.currency_id', 'company_id.currency_id', 'state', 'review_currency_id')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_currency_id(self):
        for claim in self:
            if claim.state == 'under_review' and claim.review_currency_id:
                claim.currency_id = claim.review_currency_id
            elif claim.purchase_id:
                try:
                    claim.purchase_id.check_access('read')
                    claim.currency_id = claim.purchase_id.currency_id
                except AccessError:
                    claim.currency_id = False
            else:
                claim.currency_id = claim.company_id.currency_id

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        purchase_id = values.get('purchase_id') or self.env.context.get('default_purchase_id')
        if purchase_id:
            order = self.env['purchase.order'].browse(purchase_id)
            order._check_shortage_claim_source_access()
            if 'partner_id' in field_names and not self.env.context.get('default_partner_id'):
                values['partner_id'] = order.partner_id.id
            if 'company_id' in field_names and not self.env.context.get('default_company_id'):
                values['company_id'] = order.company_id.id
        return values

    @api.onchange('purchase_id')
    def _onchange_purchase_id(self):
        if self.purchase_id and self.state == 'draft':
            self.purchase_id._check_shortage_claim_source_access()
            # Only fill missing inputs; choosing another order never discards existing choices.
            if not self.partner_id:
                self.partner_id = self.purchase_id.partner_id
            if not self._origin.id:
                self.company_id = self.purchase_id.company_id

    @api.depends('claim_line_ids.product_id')
    def _compute_product_ids(self):
        for claim in self:
            claim.product_ids = claim.claim_line_ids.product_id

    def _search_product_ids(self, operator, value):
        return [('claim_line_ids.product_id', operator, value)]

    def _purchase_context_values(self):
        self.ensure_one()
        order = self.purchase_id
        order._check_shortage_claim_source_access()
        return {'purchase_id': order.id, 'partner_id': order.partner_id.id, 'company_id': order.company_id.id,
                'currency_id': order.currency_id.id, 'state': order.state}

    @api.depends('purchase_id', 'state', 'review_purchase_context', 'purchase_id.state', 'purchase_id.partner_id',
                 'purchase_id.company_id', 'purchase_id.currency_id', 'purchase_id.date_order', 'purchase_id.date_approve',
                 'purchase_id.user_id', 'claim_line_ids.valuation_snapshot', 'claim_line_ids.purchase_line_id.product_qty',
                 'claim_line_ids.purchase_line_id.price_unit', 'claim_line_ids.purchase_line_id.discount',
                 'claim_line_ids.purchase_line_id.product_id.type', 'claim_line_ids.purchase_line_id.uom_id.factor',
                 'claim_line_ids.claim_uom_id.factor', 'claim_line_ids.purchase_line_id.tax_ids')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_purchase_context(self):
        for claim in self:
            claim.purchase_date = False
            claim.purchase_buyer_id = False
            claim.purchase_state = False
            claim.purchase_currency_id = False
            claim.purchase_source_status = 'unlinked'
            claim.purchase_source_warning = False
            if not claim.purchase_id:
                continue
            try:
                current = claim._purchase_context_values()
                order = claim.purchase_id
                claim.purchase_date = order.date_approve or order.date_order
                claim.purchase_buyer_id = order.user_id
                claim.purchase_state = order.state
                claim.purchase_currency_id = order.currency_id
                claim.purchase_source_status = 'current'
                if claim.state == 'under_review':
                    if not claim.review_purchase_context or current != claim.review_purchase_context:
                        raise ValidationError(claim.env._('The purchase source differs from the submitted basis, or no earlier source check was recorded. Return to draft for correction and re-review.'))
                    claim.claim_line_ids._check_submitted_purchase_source()
                if order.state != 'purchase':
                    claim.purchase_source_status = 'changed'
                    claim.purchase_source_warning = claim.env._('Only confirmed Purchase Orders can support review submission. RFQs, approval-pending and cancelled orders remain ineligible.')
            except AccessError:
                claim.purchase_source_status = 'unavailable'
                claim.purchase_source_warning = claim.env._('Purchase source is unavailable with your current access or activated company scope.')
            except ValidationError as error:
                claim.purchase_source_status = 'changed'
                claim.purchase_source_warning = str(error)

    def action_open_purchase_order(self):
        self.ensure_one()
        self.check_access('read')
        if not self.purchase_id:
            raise ValidationError(self.env._('Select a purchase order first.'))
        self.purchase_id._check_shortage_claim_source_access()
        return {'type': 'ir.actions.act_window', 'name': self.env._('Purchase Order'),
                'res_model': 'purchase.order', 'view_mode': 'form', 'views': [(False, 'form')],
                'res_id': self.purchase_id.id, 'target': 'current', 'context': {'create': False}}

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access('create')
        with self.env.cr.savepoint():
            prepared = []
            for values in vals_list:
                vals = dict(values)
                if vals.get('state', 'draft') != 'draft' or {'review_requested_by', 'review_requested_at', 'currency_id', 'review_currency_id', 'review_purchase_context', 'product_ids', 'purchase_date', 'purchase_buyer_id', 'purchase_state', 'purchase_currency_id', 'purchase_source_status', 'purchase_source_warning', 'audit_event_ids', 'create_uid', 'create_date', 'write_uid', 'write_date', 'review_purchase_context', 'product_ids', 'purchase_date', 'purchase_buyer_id', 'purchase_state', 'purchase_currency_id', 'purchase_source_status', 'purchase_source_warning'} & vals.keys():
                    raise AccessError(self.env._('Workflow, audit and metadata fields are controlled by the server.'))
                purchase_id = vals.get('purchase_id', self.env.context.get('default_purchase_id'))
                if purchase_id:
                    order = self.env['purchase.order'].browse(purchase_id)
                    order._check_shortage_claim_source_access()
                    vals.setdefault('purchase_id', order.id)
                    vals.setdefault('partner_id', self.env.context.get('default_partner_id') or order.partner_id.id)
                    vals.setdefault('company_id', self.env.context.get('default_company_id') or order.company_id.id)
                company_id = vals.get('company_id') or self.default_get(['company_id']).get('company_id')
                if not self.env.su and company_id not in self.env.companies.ids:
                    raise AccessError(self.env._('Select an activated company you are authorized to use.'))
                company = self.env['res.company'].browse(company_id)
                vals.update(company_id=company_id, state='draft', review_requested_by=False, review_requested_at=False, review_currency_id=False, review_purchase_context=False, audit_event_ids=[])
                vals['name'] = self.env['ir.sequence'].with_company(company).next_by_code(
                    'supplier.shortage.claim', sequence_date=vals.get('claim_date') or fields.Date.context_today(self))
                if not vals['name'] or vals['name'] == 'New':
                    raise ValidationError(self.env._('Claim reference sequence is missing or invalid.'))
                prepared.append(vals)
            clean = self.with_context({key: value for key, value in self.env.context.items() if key not in {'default_create_uid', 'default_create_date', 'default_write_uid', 'default_write_date'}})
            claims = super(ShortageClaim, clean).create(prepared)
            claims.invalidate_recordset(['currency_id', 'product_ids', 'purchase_date', 'purchase_buyer_id', 'purchase_state', 'purchase_currency_id', 'purchase_source_status', 'purchase_source_warning'])
            claims._audit('created', {claim.id: self.env._('Claim created in draft. Purchase Order: %s; Supplier: %s.', claim.purchase_id.display_name or '-', claim.partner_id.display_name) for claim in claims}, new_state='draft')
            return claims

    def _audit(self, event_type, summary, **kwargs):
        self.env['supplier.shortage.claim.audit']._append(self, event_type, summary, **kwargs)

    def _lock_for_edit(self):
        self.check_access('write')
        self.lock_for_update()
        self.invalidate_recordset(['state', 'claim_line_ids'])

    def _touch_for_line_change(self):
        self._lock_for_edit()
        if any(claim.state != 'draft' for claim in self):
            raise ValidationError(self.env._('Return the claim to draft before changing its lines.'))
        # Version the parent row so concurrent submission retries against fresh lines.
        super().write({'write_date': fields.Datetime.now()})

    def write(self, vals):
        self._lock_for_edit()
        if vals.get('purchase_id'):
            self.env['purchase.order'].browse(vals['purchase_id'])._check_shortage_claim_source_access()
        if {'state', 'review_requested_by', 'review_requested_at', 'review_currency_id', 'review_purchase_context', 'product_ids', 'purchase_date', 'purchase_buyer_id', 'purchase_state', 'purchase_currency_id', 'purchase_source_status', 'purchase_source_warning', 'audit_event_ids', 'create_uid', 'create_date', 'write_uid', 'write_date', 'review_purchase_context', 'product_ids', 'purchase_date', 'purchase_buyer_id', 'purchase_state', 'purchase_currency_id', 'purchase_source_status', 'purchase_source_warning'} & vals.keys():
            raise AccessError(self.env._('Workflow, audit and metadata fields are controlled by the server.'))
        if 'name' in vals:
            raise ValidationError(self.env._('Claim references cannot be changed.'))
        if 'currency_id' in vals:
            raise ValidationError(self.env._('Claim currency is controlled by the purchase order, or the company when no order is selected.'))
        if 'company_id' in vals and any(claim.company_id.id != vals['company_id'] for claim in self):
            raise ValidationError(self.env._('A claim company cannot change after creation. Create a separate claim in the correct company.'))
        protected = {'company_id', 'partner_id', 'purchase_id', 'user_id', 'claim_date', 'claim_line_ids', 'note'}
        if protected & vals.keys() and any(claim.state != 'draft' for claim in self):
            raise ValidationError(self.env._('Return the claim to draft before changing its business information. Use chatter for review comments.'))
        tracked = {'partner_id', 'purchase_id', 'user_id', 'claim_date'} & vals.keys()
        before = {claim.id: claim._audit_values(tracked) for claim in self}
        with self.env.cr.savepoint():
            result = super().write(vals)
            summaries = {}
            for claim in self:
                after = claim._audit_values(tracked)
                changes = ['%s: %s -> %s' % (self._fields[key].string, self._audit_label(before[claim.id], after, key, False), self._audit_label(before[claim.id], after, key, True))
                           for key in sorted(tracked) if before[claim.id]['_identity'][key] != after['_identity'][key]]
                if changes:
                    summaries[claim.id] = '\n'.join(changes)
            self.browse(list(summaries))._audit('fields_changed', summaries)
            return result

    def _audit_values(self, names):
        self.ensure_one()
        values = {name: self[name].display_name if self._fields[name].relational else str(self[name] or '') for name in names}
        values['_identity'] = {name: self[name].id if self._fields[name].relational else values[name] for name in names}
        return values

    def copy(self, default=None):
        self.check_access('read')
        return super().copy(default)

    def unlink(self):
        self.check_access('unlink')
        raise AccessError(self.env._('Claim deletion is disabled to preserve business history.'))

    @api.constrains('purchase_id', 'partner_id', 'company_id')
    def _check_purchase(self):
        for claim in self:
            if claim.purchase_id:
                claim.purchase_id._check_shortage_claim_source_access()
                if claim.purchase_id.company_id != claim.company_id:
                    raise ValidationError(self.env._('Selected purchase order belongs to another company.'))
                if claim.purchase_id.partner_id != claim.partner_id:
                    raise ValidationError(self.env._('Supplier must match the selected purchase order.'))
            claim.claim_line_ids._check_company()
            claim.claim_line_ids._check_purchase_line()

    def _review_lines(self):
        return self.claim_line_ids.filtered(lambda line: getattr(line, 'allocation_state', False) != 'released')

    def _validate_for_review(self):
        for claim in self:
            if not claim.company_id or not claim.partner_id or not claim.purchase_id or not claim.claim_date:
                raise ValidationError(self.env._('Set the company, supplier, purchase order and claim date before submitting for review.'))
            claim._check_company()
            claim._check_purchase()
            if claim.purchase_id.state != 'purchase':
                raise ValidationError(self.env._('Confirm the Purchase Order before submitting this claim for review. RFQs, approval-pending and cancelled orders are not eligible.'))
            if not claim._review_lines():
                raise ValidationError(self.env._('Add at least one claim line before submitting for review.'))
            for line in claim._review_lines():
                if not line.purchase_line_id or not line.product_id or not line.product_uom_id:
                    raise ValidationError(self.env._('Each review line needs a purchase line, product and purchasing UoM.'))
            claim.claim_line_ids._check_proposed_qty()

    def action_submit_for_review(self):
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_user'):
            raise AccessError(self.env._('Only an authorized purchase claim user can submit a claim for review.'))
        with self.env.cr.savepoint():
            self._lock_for_edit()
            if any(claim.state != 'draft' for claim in self):
                raise ValidationError(self.env._('Only draft claims can be submitted for review.'))
            self._validate_for_review()
            captured_at = fields.Datetime.now()
            details = self._review_lines()._capture_valuation_snapshot(captured_at)
            basis = {claim.id: {str(line.id): line.valuation_snapshot for line in claim._review_lines()} for claim in self}
            for claim in self:
                super(ShortageClaim, claim).write({'state': 'under_review', 'review_requested_by': self.env.uid,
                    'review_requested_at': captured_at, 'review_currency_id': claim.purchase_id.currency_id.id, 'review_purchase_context': claim._purchase_context_values()})
            self._audit('submitted', self.env._('Claim submitted for internal review.'),
                        old_state='draft', new_state='under_review', details=details, basis=basis)
        return True

    def action_return_to_draft(self):
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_manager'):
            raise AccessError(self.env._('Only an authorized purchase claim manager can return this claim to draft.'))
        with self.env.cr.savepoint():
            self._lock_for_edit()
            if any(claim.state != 'under_review' for claim in self):
                raise ValidationError(self.env._('Only claims under review can be returned to draft.'))
            super().write({'state': 'draft'})
            self._audit('returned', self.env._('Claim returned to draft for correction.'), old_state='under_review', new_state='draft',
                        reason=self.env._('Returned by an authorized purchase claim manager for draft correction.'))
        return True

    def action_refresh_provisional_valuation(self):
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_user'):
            raise AccessError(self.env._('Only an authorized purchase claim user can refresh draft valuation.'))
        with self.env.cr.savepoint():
            self._lock_for_edit()
            if any(claim.state != 'draft' for claim in self):
                raise ValidationError(self.env._('Return the claim to draft before refreshing valuation.'))
            self._check_company()
            self._check_purchase()
            self.claim_line_ids._check_proposed_qty()
            self.claim_line_ids.invalidate_recordset(list(self.claim_line_ids._valuation_output_fields - {'valuation_snapshot', 'review_purchase_uom_id'}))
            self.claim_line_ids.mapped('valuation_status')
            basis = {claim.id: {str(line.id): {**line._get_live_valuation(), 'currency_id': line.currency_id.id,
                'company_id': line.company_id.id, 'proposed_qty': line.proposed_qty,
                'claim_uom_id': (line.claim_uom_id or line.purchase_line_id.uom_id).id,
                'purchase_line_id': line.purchase_line_id.id} for line in claim._review_lines()} for claim in self}
            self._audit('valuation_refreshed', self.env._('Draft provisional valuation refreshed from current purchase data; submitted bases remain unchanged.'), basis=basis)
        return True

    def export_data(self, fields_to_export):
        self.check_access('read')
        return super().export_data(fields_to_export)

    @api.model
    def _audit_label(self, before, after, name, new):
        values = after if new else before
        label = values[name]
        if before[name] == after[name] and before['_identity'][name] != after['_identity'][name]:
            return '%s (record %s)' % (label, values['_identity'][name])
        return label
