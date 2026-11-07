import math
from decimal import Decimal, InvalidOperation

from markupsafe import Markup

from odoo import api, fields, models
from odoo.tools.float_utils import float_compare
from odoo.exceptions import AccessError, UserError, ValidationError


class ShortageClaimLine(models.Model):
    _name = 'supplier.shortage.claim.line'
    _description = 'Supplier Shortage Claim Line'
    _check_company_auto = True

    claim_id = fields.Many2one('supplier.shortage.claim', required=True, ondelete='cascade', check_company=True, index=True)
    company_id = fields.Many2one('res.company', related='claim_id.company_id', store=True, readonly=True, index=True)
    purchase_line_id = fields.Many2one('purchase.order.line', check_company=True, ondelete='restrict')
    product_id = fields.Many2one('product.product', check_company=True, ondelete='restrict')
    product_uom_id = fields.Many2one('uom.uom', string='Purchasing UoM', compute='_compute_valuation')
    claim_uom_id = fields.Many2one('uom.uom', string='Claim UoM', ondelete='restrict',
                                  help='Blank means the purchasing UoM. Conversion requires a common reference unit.')
    proposed_qty = fields.Float(string='Proposed Quantity', digits='Product Unit',
                                help='Manually proposed quantity in Claim UoM (or purchasing UoM when blank); not a verified shortage.')
    currency_id = fields.Many2one('res.currency', related='claim_id.currency_id', readonly=True, compute_sudo=False)
    note = fields.Text(string='Notes')

    purchase_qty = fields.Float(string='Ordered Quantity', digits=0, compute='_compute_valuation', aggregator=False)
    normalized_purchase_qty = fields.Float(string='Proposed in Purchase UoM', digits=0, compute='_compute_valuation', aggregator=False)
    source_unit_price = fields.Float(string='Source Unit Price', digits=0, min_display_digits='Product Price', compute='_compute_valuation', aggregator=False)
    source_discount = fields.Float(string='Discount (%)', digits='Discount', compute='_compute_valuation', aggregator=False)
    source_tax_mode = fields.Selection([('tax_excluded', 'Tax Excluded'), ('tax_included', 'Tax Included')],
                                       string='PO Price Tax Basis', compute='_compute_valuation')
    provisional_unit_price = fields.Float(string='Provisional Net Unit Price', digits=0, min_display_digits='Product Price', compute='_compute_valuation', aggregator=False)
    provisional_untaxed_amount = fields.Monetary(string='Provisional Untaxed Estimate', currency_field='currency_id',
                                               compute='_compute_valuation', aggregator=False)
    valuation_status = fields.Selection([('estimated', 'Provisional Estimate'), ('needs_review', 'Needs Valuation Review')],
                                       compute='_compute_valuation')
    valuation_note = fields.Char(compute='_compute_valuation', string='Valuation Information')
    review_purchase_uom_id = fields.Many2one('uom.uom', readonly=True, copy=False, ondelete='restrict')
    valuation_snapshot = fields.Json(readonly=True, copy=False)
    valuation_snapshot_at = fields.Datetime(string='Last Submitted Valuation', compute='_compute_submitted_valuation')
    snapshot_currency_id = fields.Many2one('res.currency', compute='_compute_submitted_valuation')
    submitted_valuation_status = fields.Selection([('estimated', 'Provisional Estimate'), ('needs_review', 'Needs Valuation Review')], compute='_compute_submitted_valuation')
    submitted_untaxed_amount = fields.Monetary(string='Last Submitted Untaxed Estimate', currency_field='snapshot_currency_id',
                                              compute='_compute_submitted_valuation', aggregator=False)

    _valuation_output_fields = frozenset({
        'purchase_qty', 'normalized_purchase_qty', 'source_unit_price', 'source_discount', 'source_tax_mode',
        'provisional_unit_price', 'provisional_untaxed_amount', 'valuation_status', 'valuation_note',
        'valuation_snapshot', 'review_purchase_uom_id', 'valuation_snapshot_at', 'snapshot_currency_id', 'submitted_untaxed_amount', 'submitted_valuation_status',
    })

    @api.depends('valuation_snapshot')
    def _compute_submitted_valuation(self):
        for line in self:
            snapshot = line.valuation_snapshot or {}
            line.submitted_valuation_status = snapshot.get('valuation_status')
            line.valuation_snapshot_at = snapshot.get('captured_at')
            line.snapshot_currency_id = snapshot.get('currency_id')
            line.submitted_untaxed_amount = snapshot.get('provisional_untaxed_amount', 0)

    @api.depends('proposed_qty', 'claim_uom_id.factor', 'claim_uom_id.parent_path', 'valuation_snapshot', 'claim_id.state',
                 'claim_id.currency_id', 'purchase_line_id.product_qty', 'purchase_line_id.price_unit',
                 'purchase_line_id.discount', 'purchase_line_id.uom_id.factor', 'purchase_line_id.uom_id.parent_path',
                 'purchase_line_id.currency_id', 'purchase_line_id.product_id', 'purchase_line_id.document_tax_mode',
                 'purchase_line_id.tax_ids', 'purchase_line_id.tax_ids.amount', 'purchase_line_id.tax_ids.amount_type',
                 'purchase_line_id.tax_ids.price_include_override', 'purchase_line_id.tax_ids.children_tax_ids',
                 'company_id.tax_calculation_rounding_method', 'company_id.account_price_include')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_valuation(self):
        for line in self:
            if line.claim_id.state == 'under_review':
                data = line.valuation_snapshot or {
                    'valuation_status': 'needs_review',
                    'valuation_note': line.env._('No valuation was captured by the earlier module version. Return to draft and re-review.'),
                }
            else:
                data = line._get_live_valuation()
            for name in ('purchase_qty', 'normalized_purchase_qty', 'source_unit_price', 'source_discount',
                         'provisional_unit_price', 'provisional_untaxed_amount'):
                line[name] = data.get(name, 0)
            line.product_uom_id = data.get('purchase_uom_id') or False
            line.source_tax_mode = data.get('source_tax_mode')
            line.valuation_status = data['valuation_status']
            line.valuation_note = data.get('valuation_note', False)

    def _normalized_quantity(self):
        self.ensure_one()
        purchase_uom = self.purchase_line_id.uom_id
        entered_uom = self.claim_uom_id or purchase_uom
        if not purchase_uom:
            if self.claim_uom_id:
                raise ValidationError(self.env._('Select a purchase line before choosing a claim UoM.'))
            return self.proposed_qty
        if not entered_uom._has_common_reference(purchase_uom):
            raise ValidationError(self.env._('Claim UoM and purchasing UoM must share a configured reference unit. Packaging names do not define a conversion.'))
        if any(not math.isfinite(uom.factor) or uom.factor <= 0 for uom in entered_uom | purchase_uom):
            raise ValidationError(self.env._('The configured UoM conversion factors must be finite and positive.'))
        # Never round a conversion upward into an artificial shortage.
        quantity = entered_uom._compute_quantity(self.proposed_qty, purchase_uom, round=False)
        if not math.isfinite(quantity) or purchase_uom.compare(quantity, 0) <= 0:
            raise ValidationError(self.env._('Converted proposed quantity is zero at Product Unit precision. Use a suitable configured precision.'))
        return quantity

    def _get_live_valuation(self):
        self.ensure_one()
        po = self.purchase_line_id
        data = {'valuation_status': 'needs_review', 'valuation_note': self.env._('Select a complete purchase line and a positive proposed quantity.')}
        if not po:
            return data
        try:
            po.order_id._check_shortage_claim_source_access()
            po._check_shortage_claim_eligible()
        except AccessError:
            data['valuation_note'] = self.env._('Purchase source is unavailable with your current permissions.')
            return data
        except ValidationError as error:
            data['valuation_note'] = str(error)
            return data
        po = po.with_company(self.company_id)
        data.update(purchase_qty=po.product_qty, purchase_uom_id=po.uom_id.id, source_unit_price=po.price_unit,
                    source_discount=po.discount, source_tax_mode=po.document_tax_mode)
        # Legacy incomplete drafts remain readable without invented estimates.
        if po.uom_id.compare(self.proposed_qty, 0) <= 0:
            return data
        normalized = self._normalized_quantity()
        data['normalized_purchase_qty'] = normalized
        if po.currency_id != self.currency_id or po.company_id != self.company_id:
            raise ValidationError(self.env._('Claim currency and company must match the purchase order before valuation.'))
        taxes = po.tax_ids.flatten_taxes_hierarchy()
        if any(tax.company_id and tax.company_id != self.company_id for tax in taxes):
            raise ValidationError(self.env._('Purchase taxes must belong to the claim company.'))
        if (not all(math.isfinite(value) for value in (po.price_unit, po.discount))
                or po.price_unit < 0 or po.discount < 0 or po.discount > 100
                or any(tax.amount_type not in ('percent', 'fixed', 'division') for tax in taxes)):
            data['valuation_note'] = self.env._('Purchase pricing or tax configuration requires manual valuation review.')
            return data
        Tax = self.env['account.tax'].with_company(self.company_id)
        try:
            base_line = po._prepare_base_line_for_taxes_computation()
            base_line['quantity'] = normalized
            Tax._add_tax_details_in_base_line(base_line, self.company_id)
            Tax._round_base_lines_tax_details([base_line], self.company_id)
            details = base_line['tax_details']
            amount = details['total_excluded_currency']
            unit_price = details['raw_total_excluded_currency'] / normalized
            if not all(math.isfinite(value) for value in (amount, unit_price)) or amount < 0:
                raise ValueError('Invalid tax valuation result')
        except AccessError:
            raise
        except (UserError, ValueError, ZeroDivisionError, OverflowError):
            data['valuation_note'] = self.env._('Odoo could not reliably value this tax configuration. A financial reviewer must inspect it.')
            return data
        data.update(provisional_unit_price=unit_price, provisional_untaxed_amount=amount,
                    valuation_status='estimated', valuation_note=self.env._('Provisional estimate - pending receipt and bill reconciliation.'))
        return data

    def _capture_valuation_snapshot(self, captured_at):
        self.check_access('write')
        self._lock_draft_claims()
        bodies = {}
        for line in self:
            data = line._get_live_valuation()
            po = line.purchase_line_id
            uom = line.claim_uom_id or po.uom_id
            data.update(captured_at=fields.Datetime.to_string(captured_at), company_id=line.company_id.id,
                        currency_id=line.currency_id.id, currency_rounding=line.currency_id.rounding, purchase_line_id=po.id, product_id=line.product_id.id,
                        proposed_qty=line.proposed_qty, claim_uom_id=uom.id, claim_uom_factor=uom.factor,
                        purchase_uom_factor=po.uom_id.factor, purchase_uom_name=po.uom_id.display_name,
                        claim_uom_name=uom.display_name, currency_name=line.currency_id.name,
                        purchase_name=po.order_id.name, product_name=line.product_id.display_name,
                        taxes=[{'id': tax.id, 'name': tax.name, 'type': tax.amount_type, 'amount': tax.amount,
                                'price_include': tax._is_price_included(document_tax_mode=po.document_tax_mode),
                                'price_include_override': tax.price_include_override, 'sequence': tax.sequence,
                                'include_base_amount': tax.include_base_amount, 'is_base_affected': tax.is_base_affected}
                               for tax in po.tax_ids.flatten_taxes_hierarchy()])
            # Private workflow path; generic create/write cannot supply snapshot data.
            super(ShortageClaimLine, line).write({'valuation_snapshot': data, 'claim_uom_id': uom.id, 'review_purchase_uom_id': po.uom_id.id})
            body = Markup('<p>%s</p><ul><li>%s: %s / %s</li><li>%s: %s %s to %s %s</li>'
                          '<li>%s: %s to %s</li><li>%s: %s %s; %s: %s%%; %s: %s</li>'
                          '<li>%s: %s</li><li>%s: %s %s (%s)</li></ul>') % (
                line.env._('Submitted provisional valuation basis (not verified recovery).'),
                line.env._('Purchase / product'), data['purchase_name'], data['product_name'],
                line.env._('Proposed quantity'), data['proposed_qty'], data['claim_uom_name'],
                data.get('normalized_purchase_qty', 0), data['purchase_uom_name'],
                line.env._('UoM reference factors'), data['claim_uom_factor'], data['purchase_uom_factor'],
                line.env._('Source unit price'), data.get('source_unit_price', 0), data['currency_name'],
                line.env._('Discount'), data.get('source_discount', 0), line.env._('PO tax mode'), data.get('source_tax_mode'),
                line.env._('Tax basis'), '; '.join('%s: %s (%s, %s)' % (tax['name'], tax['amount'], tax['type'],
                    line.env._('included') if tax['price_include'] else line.env._('excluded')) for tax in data['taxes']) or line.env._('No taxes'),
                line.env._('Provisional untaxed estimate'),
                data.get('provisional_untaxed_amount') if data['valuation_status'] == 'estimated' else line.env._('Unavailable'),
                data['currency_name'], data['valuation_note'])
            bodies[line.claim_id.id] = bodies.get(line.claim_id.id, Markup('')) + body
        return bodies

    @api.model
    def _check_controlled_values(self, vals):
        if ({'company_id', 'currency_id', 'product_uom_id', 'create_uid', 'create_date', 'write_uid', 'write_date'} | self._valuation_output_fields) & vals.keys():
            raise ValidationError(self.env._('Line company, currency, purchasing UoM and valuation outputs are controlled by source records and the review workflow.'))
        if 'proposed_qty' in vals:
            try:
                finite = math.isfinite(float(vals['proposed_qty']))
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite:
                raise ValidationError(self.env._('Proposed claim quantity must be a finite positive number.'))
            digits = self.env['decimal.precision'].precision_get('Product Unit')
            try:
                quantity = Decimal(str(vals['proposed_qty']))
                if quantity != quantity.quantize(Decimal(1).scaleb(-digits)):
                    raise ValidationError(self.env._('Proposed quantity exceeds configured Product Unit precision; it will not be silently rounded.'))
            except InvalidOperation:
                raise ValidationError(self.env._('Proposed quantity is outside the supported numeric range.')) from None

    def _lock_draft_claims(self, extra_claims=None):
        claims = self.claim_id | (extra_claims or self.env['supplier.shortage.claim'])
        claims._lock_for_edit()
        if any(claim.state != 'draft' for claim in claims):
            raise ValidationError(self.env._('Return the claim to draft before changing its lines.'))

    @api.onchange('purchase_line_id')
    def _onchange_purchase_line_id(self):
        self.product_id = self.purchase_line_id.product_id
        if not self.claim_uom_id:
            self.claim_uom_id = self.purchase_line_id.uom_id

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            self.check_access('create')
            prepared = []
            for values in vals_list:
                vals = dict(values)
                vals.setdefault('proposed_qty', self.default_get(['proposed_qty']).get('proposed_qty', 0))
                self._check_controlled_values(vals)
                # Resolve the parent before creation, including normal ORM defaults.
                parent_id = vals.get('claim_id') or self.default_get(['claim_id']).get('claim_id')
                parent = self.env['supplier.shortage.claim'].browse(parent_id)
                parent._lock_for_edit()
                if not parent or parent.state != 'draft':
                    raise ValidationError(self.env._('Claim lines can only be added to a draft claim.'))
                if vals.get('purchase_line_id'):
                    po_line = self.env['purchase.order.line'].browse(vals['purchase_line_id'])
                    po_line._check_shortage_claim_eligible()
                    po_line.order_id._check_shortage_claim_source_access()
                    vals.setdefault('product_id', po_line.product_id.id)
                    vals.setdefault('claim_uom_id', self.default_get(['claim_uom_id']).get('claim_uom_id') or po_line.uom_id.id)
                    if not vals['claim_uom_id']:
                        vals['claim_uom_id'] = po_line.uom_id.id
                vals['valuation_snapshot'] = False
                vals['review_purchase_uom_id'] = False
                prepared.append(vals)
            clean = self.with_context({key: value for key, value in self.env.context.items() if key not in {'default_create_uid', 'default_create_date', 'default_write_uid', 'default_write_date'}})
            lines = super(ShortageClaimLine, clean).create(prepared)
            lines.invalidate_recordset(list(self._valuation_output_fields))
            lines._check_proposed_qty()
            lines.claim_id._touch_for_line_change()
            for claim, claim_lines in lines.grouped('claim_id').items():
                summary = '\n'.join('%s (PO line %s): proposed %s %s' % (line.product_id.display_name or self.env._('Unlinked line'),
                    line.purchase_line_id.id or '-', line.proposed_qty, (line.claim_uom_id or line.product_uom_id).display_name or '') for line in claim_lines)
                claim._audit('lines_added', summary)
            return lines

    def write(self, vals):
        self.check_access('write')
        self.lock_for_update()
        self.invalidate_recordset(['claim_id'])
        self._check_controlled_values(vals)
        if vals.get('purchase_line_id'):
            target_line = self.env['purchase.order.line'].browse(vals['purchase_line_id'])
            target_line._check_shortage_claim_eligible()
            target_line.order_id._check_shortage_claim_source_access()
        if 'claim_uom_id' in vals and not vals['claim_uom_id'] and self.purchase_line_id:
            raise ValidationError(self.env._('Select an explicit claim UoM for linked lines so later PO unit changes cannot reinterpret the quantity.'))
        target = self.env['supplier.shortage.claim'].browse(vals.get('claim_id'))
        self._lock_draft_claims(target)
        tracked = {'claim_id', 'purchase_line_id', 'product_id', 'proposed_qty', 'claim_uom_id'} & vals.keys()
        before = {line.id: line._audit_values(tracked) for line in self}
        old_parents = self.claim_id
        with self.env.cr.savepoint():
            result = super().write(vals)
            self._check_proposed_qty()
            parents = old_parents | self.claim_id
            parents._touch_for_line_change()
            summaries = {}
            for line in self:
                after = line._audit_values(tracked)
                changes = ['%s: %s -> %s' % (self._fields[key].string, line.claim_id._audit_label(before[line.id], after, key, False), line.claim_id._audit_label(before[line.id], after, key, True))
                           for key in sorted(tracked) if before[line.id]['_identity'][key] != after['_identity'][key]]
                if changes:
                    text = self.env._('Claim line %s: ', line.id) + '; '.join(changes)
                    if 'proposed_qty' in tracked:
                        text += ' [%s -> %s]' % (before[line.id]['_uom_label'], after['_uom_label'])
                    for parent in parents.filtered(lambda claim: claim.id in {line.claim_id.id, before[line.id].get('_claim_id')}):
                        summaries[parent.id] = summaries.get(parent.id, '') + text + '\n'
            parents.browse(list(summaries))._audit('quantity_changed' if 'proposed_qty' in tracked else 'fields_changed', summaries)
            return result

    def _audit_values(self, names):
        self.ensure_one()
        values = {name: self[name].display_name if self._fields[name].relational else str(self[name] or '') for name in names}
        values['_identity'] = {name: self[name].id if self._fields[name].relational else values[name] for name in names}
        values['_uom_label'] = (self.claim_uom_id or self.purchase_line_id.uom_id).display_name or ''
        values['_claim_id'] = self.claim_id.id
        return values

    def copy(self, default=None):
        self.check_access('read')
        return super().copy(default)

    def unlink(self):
        self.check_access('unlink')
        self._lock_draft_claims()
        return super().unlink()

    @api.constrains('claim_id', 'purchase_line_id', 'product_id')
    def _check_purchase_line(self):
        for line in self:
            if line.purchase_line_id:
                line.purchase_line_id._check_shortage_claim_eligible()
                line.purchase_line_id.order_id._check_shortage_claim_source_access()
                if line.purchase_line_id.order_id != line.claim_id.purchase_id:
                    raise ValidationError(self.env._('Claim line does not belong to the selected purchase order.'))
                if line.purchase_line_id.display_type or not line.purchase_line_id.product_id or not line.purchase_line_id.uom_id:
                    raise ValidationError(self.env._('Select a product purchase line with a purchasing UoM.'))
                if line.product_id != line.purchase_line_id.product_id:
                    raise ValidationError(self.env._('The product must match the purchase line.'))

    @api.constrains('proposed_qty', 'purchase_line_id', 'claim_uom_id')
    def _check_proposed_qty(self):
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        for line in self:
            comparison = 0
            if math.isfinite(line.proposed_qty):
                comparison = (line.product_uom_id.compare(line.proposed_qty, 0) if line.product_uom_id
                              else float_compare(line.proposed_qty, 0, precision_digits=digits))
            if comparison <= 0:
                raise ValidationError(self.env._('Proposed claim quantity must be greater than zero at the purchasing UoM precision.'))
            line._normalized_quantity()

    def export_data(self, fields_to_export):
        self.check_access('read')
        return super().export_data(fields_to_export)

    def action_open_purchase_line(self):
        self.ensure_one()
        self.check_access('read')
        if not self.purchase_line_id:
            raise ValidationError(self.env._('Select a purchase line first.'))
        self.purchase_line_id.check_access('read')
        self.purchase_line_id.order_id._check_shortage_claim_source_access()
        return {'type': 'ir.actions.act_window', 'name': self.env._('Purchase Order Line'),
                'res_model': 'purchase.order.line', 'view_mode': 'form', 'views': [(False, 'form')],
                'res_id': self.purchase_line_id.id, 'target': 'current', 'context': {'create': False}}

    def _check_submitted_purchase_source(self):
        for line in self:
            po = line.purchase_line_id
            po._check_shortage_claim_eligible()
            snapshot = line.valuation_snapshot or {}
            source_uom = line.claim_uom_id or po.uom_id
            changed = (not snapshot or snapshot.get('purchase_line_id') != po.id
                       or snapshot.get('product_id') != po.product_id.id
                       or snapshot.get('purchase_uom_id') != po.uom_id.id
                       or snapshot.get('claim_uom_id') != source_uom.id
                       or po.uom_id.compare(snapshot.get('purchase_qty', 0), po.product_qty) != 0
                       or snapshot.get('source_tax_mode') != po.document_tax_mode
                       or not source_uom._has_common_reference(po.uom_id))
            for key, value in (('source_unit_price', po.price_unit), ('source_discount', po.discount),
                               ('purchase_uom_factor', po.uom_id.factor), ('claim_uom_factor', source_uom.factor)):
                changed = changed or Decimal(str(snapshot.get(key, 0))) != Decimal(str(value))
            taxes = po.tax_ids.flatten_taxes_hierarchy()
            recorded_taxes = snapshot.get('taxes', [])
            changed = changed or [tax.id for tax in taxes] != [tax['id'] for tax in recorded_taxes]
            if not changed:
                for tax, old in zip(taxes, recorded_taxes):
                    changed = (Decimal(str(tax.amount)) != Decimal(str(old['amount']))
                               or tax.amount_type != old['type'] or tax.sequence != old['sequence']
                               or tax._is_price_included(document_tax_mode=po.document_tax_mode) != old['price_include']
                               or tax.include_base_amount != old['include_base_amount']
                               or tax.is_base_affected != old['is_base_affected'])
                    if changed:
                        break
            if changed:
                raise ValidationError(self.env._('Source purchase quantities, products, UoMs, pricing or taxes changed after submission. Submitted values are preserved; return to draft for correction and re-review.'))
