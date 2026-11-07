from odoo import api, fields, models, Command
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError, UserError


EXCEPTION_TYPES = [
    ('pending_delivery', 'Pending Supplier Delivery'),
    ('canceled_backorder_review', 'Canceled Receiving / Backorder Review'),
    ('over_receipt', 'Over-Receipt'), ('source_quantity_mismatch', 'Source Quantity Conflict'),
    ('missing_purchase_line', 'Missing Purchase Line Link'), ('incompatible_uom', 'Incompatible Receiving UoM'),
    ('unexpected_return', 'Unexpected Supplier Return'), ('ambiguous_stock_movement', 'Ambiguous Stock Movement'),
    ('inconsistent_backorder', 'Inconsistent Backorder'), ('receiving_source_changed', 'Receiving Source Changed'),
    ('other_review', 'Receiving Review Required'),
]
REVIEW_STATES = [('new', 'New'), ('in_review', 'In Review'), ('resolved', 'Resolved'), ('dismissed', 'Dismissed')]


class ReceivingException(models.Model):
    _name = 'supplier.shortage.receipt.exception'
    _description = 'Receiving Exception Review'
    _order = 'last_detected_at desc, id desc'
    _check_company_auto = True
    _allow_sudo_commands = False

    name = fields.Char(required=True, readonly=True, copy=False)
    source_key = fields.Char(required=True, readonly=True, copy=False, index=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    purchase_id = fields.Many2one('purchase.order', required=True, readonly=True, check_company=True, ondelete='restrict', index=True)
    purchase_line_id = fields.Many2one('purchase.order.line', readonly=True, check_company=True, ondelete='restrict', index=True)
    claim_id = fields.Many2one('supplier.shortage.claim', readonly=True, check_company=True, ondelete='restrict')
    partner_id = fields.Many2one(related='purchase_id.partner_id', compute_sudo=False, store=True, string='Supplier')
    product_id = fields.Many2one(related='purchase_line_id.product_id', compute_sudo=False)
    product_uom_id = fields.Many2one('uom.uom', readonly=True, ondelete='restrict')
    source_move_ids = fields.Many2many('stock.move', string='Source Movements', readonly=True)
    exception_type = fields.Selection(EXCEPTION_TYPES, required=True, readonly=True, index=True)
    severity = fields.Selection([('info', 'Information'), ('warning', 'Warning'), ('high', 'High')], required=True, readonly=True)
    review_state = fields.Selection(REVIEW_STATES, required=True, readonly=True, index=True, default='new', copy=False)
    condition_present = fields.Boolean(readonly=True, default=True, index=True)
    assigned_user_id = fields.Many2one('res.users', string='Assigned Employee', check_company=True)
    affected_quantity = fields.Float(readonly=True, digits=0, aggregator=False)
    quantity_known = fields.Boolean(readonly=True)
    description = fields.Text(required=True, readonly=True)
    first_detected_at = fields.Datetime(required=True, readonly=True)
    last_detected_at = fields.Datetime(required=True, readonly=True, string='Last Material Detection')
    source_signature = fields.Char(required=True, readonly=True)
    review_reason = fields.Text(string='Review Notes / Decision Reason')
    resolved_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    resolved_at = fields.Datetime(readonly=True)
    last_decision_reason = fields.Text(readonly=True)
    can_manage_review = fields.Boolean(compute='_compute_review_permissions')

    @api.depends_context('uid')
    def _compute_review_permissions(self):
        for rec in self:
            rec.can_manage_review = rec._is_review_manager()
    audit_event_ids = fields.One2many('supplier.shortage.claim.audit', 'receipt_exception_id', readonly=True)

    _reference_unique = models.Constraint('UNIQUE(name)', 'Receiving exception references must be unique.')
    _source_unique = models.Constraint('UNIQUE(source_key)', 'This receiving condition already has a review record. Refresh again.')

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        # Stored review quantities must not outlive access to their supporting evidence.
        for model in ('stock.move', 'stock.picking', 'stock.move.line', 'stock.warehouse'):
            if self.env[model]._access_domain('read').is_false():
                return domain & Domain('reconciliation_kind', '=', 'billing') & Domain('source_move_ids', '=', False)
        visible_move = (Domain('id', 'access', 'read') & (Domain('picking_id', '=', False) | Domain('picking_id', 'access', 'read'))
                        & Domain('location_id', 'access', 'read') & Domain('location_dest_id', 'access', 'read'))
        visible_move &= ~Domain('move_line_ids', 'any!', ~Domain('id', 'access', 'read'))
        visible_move &= (~Domain('picking_type_id.warehouse_id', '!=', False)
                         | Domain('picking_type_id.warehouse_id', 'access', 'read'))
        for path in ('location_id.warehouse_id', 'location_dest_id.warehouse_id'):
            visible_move &= (Domain(path, '=', False) | Domain(path, 'access', 'read'))
        domain &= ~Domain('source_move_ids', 'any!', ~visible_move)
        domain &= (~Domain('purchase_line_id', '!=', False) | Domain('purchase_line_id', 'access', 'read'))
        return domain

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._('Receiving exceptions are generated only by authorized reconciliation.'))

    def copy(self, default=None):
        raise AccessError(self.env._('Receiving exceptions cannot be duplicated.'))

    def unlink(self):
        raise AccessError(self.env._('Receiving review history cannot be deleted.'))

    def _is_review_manager(self):
        return self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_manager')

    def _require_operator(self, manager=False):
        role = 'group_claim_manager' if manager else 'group_claim_user'
        allowed = self.env.user.has_group('supplier_shortage_claim_reconciliation.' + role)
        if not manager:
            allowed |= self.env.user.has_group('supplier_shortage_claim_reconciliation.group_warehouse_user')
        if not allowed:
            raise AccessError(self.env._('Only an authorized receiving reviewer can perform this operation.'))
        self.check_access('write')

    def _validate_assignee(self, user):
        if user:
            user.check_access('read')
            if not user.active or user.share or any(rec.company_id not in user.company_ids for rec in self):
                raise ValidationError(self.env._('Assign an active internal reviewer authorized for this company.'))
            if not any(user.has_group('supplier_shortage_claim_reconciliation.' + role)
                       for role in ('group_claim_user', 'group_warehouse_user')):
                raise ValidationError(self.env._('The assigned employee must have a receiving review role.'))

    def write(self, vals):
        if not vals:
            return True
        if vals.keys() - {'assigned_user_id', 'review_reason'}:
            raise AccessError(self.env._('Exception evidence, workflow and audit fields are server-controlled.'))
        with self.env.cr.savepoint():
            self._require_operator()
            self.lock_for_update()
            self.invalidate_recordset()
            if any(rec.review_state not in ('new', 'in_review') for rec in self) and not (vals.keys() == {'review_reason'} and self._is_review_manager()):
                raise ValidationError(self.env._('Reopen the exception before changing its review information.'))
            if 'assigned_user_id' in vals:
                if not self._is_review_manager():
                    raise AccessError(self.env._('Only a purchase claim manager may assign exceptions.'))
                self._validate_assignee(self.env['res.users'].browse(vals['assigned_user_id']))
            if any(rec.assigned_user_id and rec.assigned_user_id != self.env.user for rec in self) and not self._is_review_manager():
                raise AccessError(self.env._('Only the assigned reviewer or a purchase claim manager may change review notes.'))
            changes = {rec.id: self.env._('Review information changed: %s', ', '.join('%s: %s -> %s' % (key, rec[key].display_name if key == 'assigned_user_id' and rec[key] else rec[key], value) for key,value in vals.items() if (rec[key].id if key == 'assigned_user_id' else rec[key]) != value)) for rec in self}
            changed = self.filtered(lambda rec: any((rec[key].id if key == 'assigned_user_id' else rec[key]) != value for key,value in vals.items()))
            result = super().write(vals)
            if changed:
                self.env['supplier.shortage.claim.audit']._append_exceptions(changed,
                    'exception_assigned' if 'assigned_user_id' in vals else 'exception_noted', changes, vals.get('review_reason'))
            return result

    def _transition(self, target, event):
        with self.env.cr.savepoint():
            self._require_operator(manager=target != 'in_review')
            self.lock_for_update()
            self.invalidate_recordset()
            allowed = ('new',) if target == 'in_review' else (('resolved', 'dismissed') if target == 'new' else ('new', 'in_review'))
            if any(rec.review_state not in allowed for rec in self):
                raise ValidationError(self.env._('This review transition is not available from the current state.'))
            if target == 'in_review' and any(rec.assigned_user_id and rec.assigned_user_id != self.env.user for rec in self) and not self._is_review_manager():
                raise AccessError(self.env._('Only the assigned reviewer may start this review.'))
            if target != 'in_review' and any(len((rec.review_reason or '').strip()) < 5 for rec in self):
                raise ValidationError(self.env._('Record a meaningful review or decision reason first.'))
            if target == 'new' and any((rec.review_reason or '').strip() == (rec.last_decision_reason or '').strip() for rec in self):
                raise ValidationError(self.env._('Record a new reason explaining why this completed review must reopen.'))
            summaries = {rec.id: self.env._('Exception review: %s -> %s.', rec.review_state, target) for rec in self}
            values = {'review_state': target}
            if target in ('resolved', 'dismissed'):
                values.update(resolved_by=self.env.uid, resolved_at=fields.Datetime.now())
            super().write(values)
            for rec in self:
                if target in ('resolved', 'dismissed'):
                    super(ReceivingException, rec).write({'last_decision_reason': rec.review_reason})
                self.env['supplier.shortage.claim.audit']._append_exceptions(rec, event, summaries[rec.id], rec.review_reason)
        return True

    def action_start_review(self):
        return self._transition('in_review', 'exception_started')

    def action_resolve(self):
        return self._transition('resolved', 'exception_resolved')

    def action_dismiss(self):
        return self._transition('dismissed', 'exception_dismissed')

    def action_reopen(self):
        return self._transition('new', 'exception_reopened')

    def action_open_sources(self):
        self.ensure_one()
        self.check_access('read')
        self.source_move_ids.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': self.env._('Receiving Source Movements'),
                'res_model': 'stock.move', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.source_move_ids.ids)],
                'views': [(self.env.ref('supplier_shortage_claim_reconciliation.view_receipt_move_list').id, 'list'),
                          (self.env.ref('supplier_shortage_claim_reconciliation.view_receipt_move_form').id, 'form')],
                'context': {'create': False, 'edit': False, 'delete': False}}

    @api.model
    def _synchronize(self, scope, findings):
        # Caller has checked source coverage and serialized the relevant POs.
        self._require_operator()
        self.check_access('create')
        existing = self.search(scope)
        by_key = {rec.source_key: rec for rec in existing}
        now = fields.Datetime.now()
        for key, values in findings.items():
            rec = by_key.get(key)
            if not rec:
                reference = self.env['ir.sequence'].with_company(self.env['res.company'].browse(values['company_id'])).next_by_code('supplier.shortage.receipt.exception')
                if not reference:
                    raise ValidationError(self.env._('Receiving exception sequence is unavailable. Contact your administrator.'))
                vals = dict(values, source_key=key, name=reference, first_detected_at=now,
                            last_detected_at=now, review_state='new', condition_present=True,
                            assigned_user_id=self.env.uid, review_reason=False, resolved_by=False, resolved_at=False)
                rec = super(ReceivingException, self.with_context({}, allowed_company_ids=self.env.companies.ids)).create(vals)
                self.env['supplier.shortage.claim.audit']._append_exceptions(rec, 'exception_detected', rec.description)
            elif rec.source_signature != values['source_signature'] or not rec.condition_present:
                old = rec.review_state
                changed = dict(values, condition_present=True, last_detected_at=now)
                if old in ('resolved', 'dismissed') or rec.reconciliation_kind == 'detection' and old == 'in_review':
                    changed['review_state'] = 'new'
                super(ReceivingException, rec).write(changed)
                self.env['supplier.shortage.claim.audit']._append_exceptions(rec,
                    'exception_reopened' if old in ('resolved', 'dismissed') else 'exception_changed',
                    self.env._('Source condition changed; previous decision and evidence remain in audit history. %s', rec.description))
        absent = existing.filtered(lambda rec: rec.source_key not in findings and rec.condition_present)
        if absent:
            super(ReceivingException, absent).write({'condition_present': False, 'last_detected_at': now})
            self.env['supplier.shortage.claim.audit']._append_exceptions(absent, 'exception_changed',
                self.env._('This condition is no longer detected. Review history and decisions remain unchanged.'))
        return self.search(scope)

    @api.constrains('company_id', 'purchase_id', 'purchase_line_id', 'source_move_ids', 'claim_id')
    def _check_source_ownership(self):
        for rec in self:
            if rec.purchase_id.company_id != rec.company_id or (rec.purchase_line_id and rec.purchase_line_id.order_id != rec.purchase_id):
                raise ValidationError(self.env._('Exception purchase-line and company ownership must match.'))
            if rec.claim_id and (rec.claim_id.company_id != rec.company_id or rec.claim_id.purchase_id != rec.purchase_id):
                raise ValidationError(self.env._('Exception claim ownership must match the purchase order.'))
            if any(move.company_id != rec.company_id for move in rec.source_move_ids):
                raise ValidationError(self.env._('Exception stock movements must belong to the same company.'))

