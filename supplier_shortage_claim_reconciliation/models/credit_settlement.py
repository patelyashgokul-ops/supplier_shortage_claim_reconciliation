import hashlib
import json
import math

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain

from .claim_preparation import ROLE, lock_records
from .shortage_claim_audit import ShortageClaimAudit

MODEL = 'supplier.shortage.credit.allocation'
INPUTS = {'claim_line_id','credit_line_id','purpose','source_amount','source_quantity','evidence_id','note','reason'}
PURPOSE = [('shortage','Shortage resolution'),('price','Price adjustment'),('return','Supplier return'),
           ('quality','Quality adjustment'),('tax','Tax only'),('rebate','Rebate or discount'),('review','Requires review')]


def credit_hold(line):
    line.env[MODEL].flush_model(['state','claim_line_id','claim_value','claim_quantity','currency_id'])
    line.env.cr.execute("""
        SELECT coalesce(sum(claim_value),0),coalesce(sum(claim_quantity),0),
               coalesce(bool_or(currency_id != %s),false)
        FROM supplier_shortage_credit_allocation WHERE claim_line_id=%s AND state='verified'
    """, [line.currency_id.id,line.id])
    return line.env.cr.fetchone()


def version_credit(env, ids):
    if ids:
        env.cr.execute("""
            INSERT INTO ssc_credit_source_version(move_id) SELECT unnest(%s::integer[])
            ON CONFLICT(move_id) DO UPDATE SET move_id=excluded.move_id
        """, [sorted(set(ids))])


def notify_credit_source(env, moves):
    if not env.registry.ready or not moves:
        return
    roots = (moves | moves.reversed_entry_id).filtered(lambda m:m.move_type=='in_refund')
    if not roots:
        return
    version_credit(env, roots.ids)
    env[MODEL].flush_model(['state','source_dirty','credit_move_id'])
    env.cr.execute("""
        WITH updated AS (
            UPDATE supplier_shortage_credit_allocation SET source_dirty=true
            WHERE credit_move_id IN %s AND state IN ('finance','verified') AND NOT source_dirty
            RETURNING id,claim_id,company_id,source_fingerprint
        )
        INSERT INTO supplier_shortage_claim_audit
          (claim_id,company_id,credit_allocation_id,event_type,actor_id,event_datetime,change_summary,valuation_basis)
        SELECT claim_id,company_id,id,'credit_event',%s,statement_timestamp() AT TIME ZONE 'UTC',
               'Credit source requires revalidation',json_build_object('fingerprint',source_fingerprint)
        FROM updated
    """, [tuple(roots.ids),env.uid])
    env[MODEL].invalidate_model(['source_dirty','effective_value','source_status'],flush=False)
    env['supplier.shortage.claim.line'].invalidate_model(
        ['credit_value','settlement_unresolved_value','settlement_review'],flush=False)
    env['supplier.shortage.claim'].invalidate_model(['verified_recovery_amount','audit_event_ids'],flush=False)


class CreditAllocation(models.Model):
    _name = MODEL
    _description = 'Vendor Credit Allocation'
    _rec_name = 'claim_id'
    _order = 'id desc'
    _check_company_auto = True
    claim_line_id = fields.Many2one('supplier.shortage.claim.line',required=True,check_company=True,ondelete='restrict',index=True)
    claim_id = fields.Many2one(related='claim_line_id.claim_id',store=True,index=True)
    company_id = fields.Many2one(related='claim_line_id.company_id',store=True,index=True)
    supplier_id = fields.Many2one(related='claim_id.partner_id')
    credit_line_id = fields.Many2one('account.move.line',required=True,check_company=True,ondelete='restrict',index=True)
    credit_move_id = fields.Many2one(related='credit_line_id.move_id',store=True,index=True,compute_sudo=False)
    source_date = fields.Date(related='credit_move_id.date',compute_sudo=False)
    source_currency_id = fields.Many2one(related='credit_line_id.currency_id',compute_sudo=False)
    currency_id = fields.Many2one('res.currency',readonly=True,copy=False)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    original_bill_id = fields.Many2one('account.move',readonly=True,ondelete='restrict',copy=False)
    approval_id = fields.Many2one('supplier.shortage.approval.cycle',readonly=True,ondelete='restrict',copy=False)
    evidence_id = fields.Many2one('supplier.shortage.claim.evidence',check_company=True,ondelete='restrict')
    purpose = fields.Selection(PURPOSE,required=True,default='review')
    source_amount = fields.Monetary(required=True,currency_field='source_currency_id',aggregator=False)
    source_quantity = fields.Float(digits='Product Unit',aggregator=False)
    source_tax = fields.Monetary(currency_field='source_currency_id',readonly=True,aggregator=False,copy=False)
    company_value = fields.Monetary(currency_field='company_currency_id',readonly=True,aggregator=False,copy=False)
    claim_value = fields.Monetary(readonly=True,aggregator=False,copy=False)
    claim_quantity = fields.Float(readonly=True,digits='Product Unit',aggregator=False,copy=False)
    effective_value = fields.Monetary(compute='_position',aggregator=False)
    available_source = fields.Monetary(compute='_position',currency_field='source_currency_id',aggregator=False)
    source_status = fields.Selection([('draft','Draft'),('posted','Posted'),('cancel','Canceled'),
        ('partial','Partially reversed'),('reversed','Reversed'),('review','Requires review')],compute='_position')
    realization = fields.Selection([('open','Not realized'),('part_payable','Partly applied to payable'),
        ('payable','Applied to payable'),('cash','Bank refund confirmed'),('mixed','Partly realized or mixed'),
        ('unknown','Requires review')],compute='_position')
    state = fields.Selection([('draft','Draft'),('posting','Pending source posting'),('accounts','Pending Accounts'),
        ('finance','Pending Finance'),('verified','Verified'),('rejected','Rejected'),('reversed','Reversed')],
        default='draft',required=True,readonly=True,index=True,copy=False)
    note = fields.Text(required=True)
    reason = fields.Text()
    source_dirty = fields.Boolean(readonly=True,copy=False,index=True)
    source_fingerprint = fields.Char(readonly=True,copy=False)
    fx_basis = fields.Json(readonly=True,copy=False)
    conversion_date = fields.Date(readonly=True,copy=False)
    accounts_by = fields.Many2one('res.users',readonly=True,ondelete='restrict',copy=False)
    verified_by = fields.Many2one('res.users',readonly=True,ondelete='restrict',copy=False)
    verified_at = fields.Datetime(readonly=True,copy=False)
    reversed_at = fields.Datetime(readonly=True,copy=False)
    reversal_ids = fields.Many2many('account.move',readonly=True,copy=False)
    active_credit_line_id = fields.Many2one('account.move.line',readonly=True,ondelete='restrict',copy=False)
    audit_ids = fields.One2many('supplier.shortage.claim.audit','credit_allocation_id',readonly=True,copy=False)

    _unique_source_claim = models.Constraint('UNIQUE(claim_line_id,active_credit_line_id)',
        'This credit line already has a verified allocation for this claim line.')

    def init(self):
        self.env.cr.execute('CREATE TABLE IF NOT EXISTS ssc_credit_source_version (move_id integer PRIMARY KEY)')

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        for name in ('claim_line_id','credit_line_id','credit_move_id'):
            domain &= Domain(name,'access','read')
        for name in ('original_bill_id','evidence_id'):
            domain &= Domain(name,'=',False) | Domain(name,'access','read')
        return domain & ~Domain('reversal_ids','any!',~Domain('id','access','read'))

    def _role(self, role):
        if self.env.su or not self.env.user.has_group(ROLE+role):
            raise AccessError('A designated credit settlement employee is required.')
        if any(row.company_id not in self.env.companies for row in self):
            raise AccessError('Activate the claim company.')

    @api.model_create_multi
    def create(self, values):
        self._role('group_claim_user')
        if any(vals.keys()-INPUTS for vals in values):
            raise AccessError('Verification and valuation fields are system controlled.')
        rows = super(CreditAllocation,self.with_context({},allowed_company_ids=self.env.companies.ids)).create(values)
        for row in rows:
            row._match()
            row._audit('Credit allocation proposed',False)
        return rows.with_env(self.env)

    def write(self, values):
        self.check_access('write')
        if values.keys()-INPUTS:
            raise AccessError('Use the credit settlement actions.')
        lock_records(self)
        self.invalidate_recordset()
        for row in self:
            if values.keys() <= {'reason'} and row.state not in ('rejected','reversed'):
                continue
            row._role('group_claim_user')
            if row.state!='draft' or row.create_uid!=self.env.user:
                raise AccessError('Only the maker can edit a draft allocation.')
        result = super().write(values)
        for row in self:
            if values.keys()-{'reason'}:
                row._match()
            row._audit('Credit allocation updated',row.state)
        return result

    def unlink(self):
        raise AccessError('Credit allocation history is retained.')

    def _match(self):
        self.ensure_one()
        self.check_access('read')
        line, credit, move = self.claim_line_id,self.credit_line_id,self.credit_move_id
        (move | move.reversed_entry_id).check_access('read')
        credit.check_access('read')
        if (move.move_type!='in_refund' or move.company_id!=line.company_id or credit.display_type!='product'
                or move.commercial_partner_id!=self.supplier_id.commercial_partner_id):
            raise ValidationError('Select a vendor credit product line for this supplier and company.')
        if credit.purchase_line_id and credit.purchase_line_id!=line.purchase_line_id:
            raise ValidationError('The credit belongs to another purchase line.')
        if credit.product_id and credit.product_id!=line.product_id:
            raise ValidationError('The credit product differs from the claim product.')
        if (len((self.note or '').strip())<20 or not math.isfinite(self.source_amount) or self.source_amount<=0
                or not math.isfinite(self.source_quantity) or self.source_quantity<0):
            raise ValidationError('Document the match and enter a positive untaxed credit amount.')
        if self.evidence_id and (self.evidence_id.claim_id!=self.claim_id or line not in self.evidence_id._scope_lines()):
            raise ValidationError('Evidence must cover the selected claim line.')
