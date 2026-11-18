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


def check_credit_identity(line, credit):
    if not line or not credit:
        raise ValidationError('Select an existing claim line and vendor credit line.')
    line.check_access('read')
    credit.check_access('read')
    move=credit.move_id
    (move | move.reversed_entry_id).check_access('read')
    if (move.move_type!='in_refund' or move.company_id!=line.company_id or credit.display_type!='product'
            or move.commercial_partner_id!=line.claim_id.partner_id.commercial_partner_id):
        raise ValidationError('Select a vendor credit product line for this supplier and company.')
    if credit.purchase_line_id and credit.purchase_line_id!=line.purchase_line_id:
        raise ValidationError('The credit belongs to another purchase line.')
    if credit.product_id and credit.product_id!=line.product_id:
        raise ValidationError('The credit product differs from the claim product.')
    return move


def approved_basis(line):
    claim=line.claim_id
    cycle=claim.approval_cycle_ids.filtered(lambda rec:rec.state=='approved' and rec.number==claim.approval_cycle)[:1]
    basis=(cycle.snapshot or {}).get('basis',{}).get('financial_lines',[]) if cycle else []
    row=next((item for item in basis if item['line_id']==line.id),{})
    if (not row or claim.state!='under_review' or claim.approval_status not in ('approved','stale')
            or claim.replacement_conflict_cycle==cycle.number or claim.purchase_source_status!='current'
            or row['currency_id']!=claim.currency_id.id or row['purchase_line_id']!=line.purchase_line_id.id):
        raise ValidationError('Reconcile the underlying source and obtain a current claim approval.')
    if claim.approval_status=='stale':
        invalidations=claim.audit_event_ids.filtered(lambda event:event.event_type=='approval_event'
            and event.event_datetime>=cycle.create_date
            and 'invalidated current internal authorization' in event.change_summary)
        if not invalidations or any((event.valuation_basis or {}).get('source_kind') not in ('receiving','credit')
                                    for event in invalidations):
            raise ValidationError('The stale approval requires financial reapproval.')
    original=line.env['account.move.line'].browse(row['bill_line_id'])
    original.check_access('read')
    original.move_id.check_access('read')
    approved_uom=line.env['uom.uom'].browse(row['uom_id'])
    if (original.move_id.state!='posted' or original.move_id.move_type!='in_invoice'
            or original.purchase_line_id!=line.purchase_line_id or original.currency_id!=cycle.currency_id
            or original.quantity!=row['invoice_quantity'] or original.price_subtotal!=row['invoice_untaxed_subtotal']
            or original.product_id!=line.product_id or original.price_unit!=row['price_unit']
            or original.discount!=row['discount'] or set(original.tax_ids.ids)!=set(row['tax_ids'])
            or original.product_uom_id.id!=row['bill_uom_id'] or original.product_uom_id.factor!=row['bill_uom_factor']
            or original.move_id.commercial_partner_id!=claim.partner_id.commercial_partner_id
            or approved_uom!=line.purchase_line_id.uom_id or approved_uom.factor!=row['uom_factor']
            or approved_uom.compare(row['quantity'],line.allocated_qty)):
        raise ValidationError('The approved original bill or quantity basis has changed.')

    if line.env['supplier.shortage.claim.dispute'].search_count([
            ('claim_id','=',claim.id),('claim_line_id','in',[False,line.id]),
            ('dispute_state','not in',('closed','resolved'))],limit=1):
        raise ValidationError('Resolve the open dispute before allocating credit.')
    return cycle,row,original.move_id


def dated_fx(env, source, target, company, date):
    detail=[]
    if source!=target:
        rates=(source | target)._get_rates(company,date)
        for currency in source | target:
            if currency==company.currency_id:
                continue
            rate,rate_date=rates[currency.id]
            record=env['res.currency.rate'].search([
                ('currency_id','=',currency.id),('name','=',rate_date),
                ('company_id','in',[False,company.root_id.id])],order='company_id.id,name desc',limit=1)
            if (not record or not rate_date or rate_date>date or not math.isfinite(rate)
                    or rate<=0 or not math.isclose(record.rate,rate,rel_tol=1e-12)):
                raise ValidationError('A documented historical FX rate is required on or before the credit accounting date.')
            detail.append({'id':record.id,'date':str(rate_date),'rate':rate,'currency':currency.id})
    rate=source._get_conversion_rate(source,target,company,date)
    if not math.isfinite(rate) or rate<=0:
        raise ValidationError('Invalid currency conversion rate.')
    return {'rate':rate,'date':str(date),'source':source.id,'target':target.id,'rates':detail}


def credit_hold(line):
    line.env[MODEL].flush_model(['state','claim_line_id','claim_value','claim_quantity','currency_id'])
    line.env.cr.execute("""
        SELECT coalesce(sum(claim_value),0),coalesce(sum(claim_quantity),0),
               coalesce(bool_or(currency_id != %s),false)
        FROM supplier_shortage_credit_allocation WHERE claim_line_id=%s AND state='verified'
    """, [line.currency_id.id,line.id])
    credit,quantity,conflict=line.env.cr.fetchone()
    from .cash_settlement import cash_hold
    cash,cash_conflict=cash_hold(line)
    return credit+cash,quantity,conflict or cash_conflict


def version_credit(env, ids):
    if ids:
        env.cr.execute("""
            INSERT INTO ssc_credit_source_version(move_id) SELECT unnest(%s::integer[])
            ON CONFLICT(move_id) DO UPDATE SET move_id=excluded.move_id
        """, [sorted(set(ids))])


def correction_origins(moves):
    parents=moves.reversed_entry_id
    return parents | moves.debit_origin_id if 'debit_origin_id' in moves._fields else parents


def accounting_corrections(moves):
    children=moves.reversal_move_ids
    return children | moves.debit_note_ids if 'debit_note_ids' in moves._fields else children


def notify_credit_source(env, moves):
    if not env.registry.ready or not moves:
        return
    roots = (moves | correction_origins(moves)).filtered(lambda m:m.move_type=='in_refund')
    _mark_credit_roots(env,roots.ids)


def _mark_credit_roots(env, ids):
    if not ids:
        return
    version_credit(env, ids)
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
    """, [tuple(ids),env.uid])
    env[MODEL].invalidate_model(['source_dirty','effective_value','source_status','available_source','realization'],flush=False)
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
    credit_line_id = fields.Many2one('account.move.line',check_company=True,ondelete='set null',index=True)
    credit_move_id = fields.Many2one(related='credit_line_id.move_id',store=True,index=True,compute_sudo=False)
    source_date = fields.Date(related='credit_move_id.date',compute_sudo=False)
    source_currency_id = fields.Many2one(related='credit_line_id.currency_id',compute_sudo=False)
    currency_id = fields.Many2one('res.currency',readonly=True,copy=False)
    company_currency_id = fields.Many2one('res.currency',readonly=True,copy=False)
    source_snapshot_currency_id = fields.Many2one('res.currency',readonly=True,copy=False)
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
    active_credit_line_id = fields.Many2one('account.move.line',readonly=True,ondelete='set null',copy=False)
    audit_ids = fields.One2many('supplier.shortage.claim.audit','credit_allocation_id',readonly=True,copy=False)

    _unique_source_claim = models.Constraint('UNIQUE(claim_line_id,active_credit_line_id)',
        'This credit line already has a verified allocation for this claim line.')

    def init(self):
        self.env.cr.execute('CREATE TABLE IF NOT EXISTS ssc_credit_source_version (move_id integer PRIMARY KEY)')

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        domain &= Domain('claim_line_id','access','read')
        if not self.env['account.move.line'].has_access('read') or not self.env['account.move'].has_access('read'):
            return Domain.FALSE
        for name in ('credit_line_id','credit_move_id','original_bill_id','evidence_id'):
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
        line,credit=self.claim_line_id,self.credit_line_id
        check_credit_identity(line,credit)
        if (len((self.note or '').strip())<20 or not math.isfinite(self.source_amount) or self.source_amount<=0
                or not math.isfinite(self.source_quantity) or self.source_quantity<0):
            raise ValidationError('Document the match and enter a positive untaxed credit amount.')
        if self.evidence_id and (self.evidence_id.claim_id!=self.claim_id or line not in self.evidence_id._scope_lines()):
            raise ValidationError('Evidence must cover the selected claim line.')

    def _source(self):
        move,line=self.credit_move_id,self.credit_line_id
        if not move or not line:
            return 'review',0,self.env['account.move'],False
        move.check_access('read')
        line.check_access('read')
        if move.state!='posted':
            return move.state,0,self.env['account.move'],False
        currency=line.currency_id
        amount=line.price_subtotal
        reversals=accounting_corrections(move)
        reversals.check_access('read')
        reversals=reversals.filtered(lambda record:record.state=='posted')
        offset=0
        signatures=[]
        for reversal in reversals.sorted('id'):
            products=reversal.invoice_line_ids.filtered(lambda item:item.display_type=='product')
            products.check_access('read')
            subsequent=accounting_corrections(reversal)
            subsequent.check_access('read')
            if (reversal.move_type!='in_invoice' or reversal.currency_id!=currency
                    or reversal.company_id!=move.company_id or reversal.commercial_partner_id!=move.commercial_partner_id
                    or subsequent.filtered(lambda item:item.state=='posted')):
                return 'review',0,reversals,False
            def matches(item):
                return item.product_id==line.product_id and item.purchase_line_id==line.purchase_line_id
            peers=move.invoice_line_ids.filtered(lambda item:item.display_type=='product' and matches(item))
            exact=products.filtered(matches)
            originals=move.invoice_line_ids.filtered(lambda item:item.display_type=='product')
            if len(peers)!=1 or len(exact)>1 or not products or any(len(originals.filtered(
                    lambda item:item.product_id==correction.product_id and item.purchase_line_id==correction.purchase_line_id
                    ))!=1 for correction in products):
                return 'review',0,reversals,False
            if exact:
                if exact.price_subtotal<0 or not math.isfinite(exact.price_subtotal):
                    return 'review',0,reversals,False
                offset+=exact.price_subtotal
            signatures.append((reversal.id,reversal.amount_untaxed,exact.id,exact.price_subtotal or 0,exact.price_total or 0))
        if not math.isfinite(amount) or currency.compare_amounts(amount,0)<=0:
            return 'review',0,reversals,False
        if currency.compare_amounts(offset,amount)>0:
            return 'review',0,reversals,False
        net=currency.round(max(amount-offset,0))
        status='reversed' if currency.is_zero(net) else 'partial' if offset else 'posted'
        fp=hashlib.sha256(json.dumps([
            move.id,move.company_id.id,move.commercial_partner_id.id,str(move.date),move.reversed_entry_id.id,move.amount_untaxed,
            currency.id,currency.rounding,line.id,line.product_id.id,line.purchase_line_id.id,amount,
            line.price_total,line.balance,line.amount_currency,line.quantity,line.product_uom_id.id,
            line.product_uom_id.factor,sorted(line.tax_ids.ids),signatures,
        ]).encode()).hexdigest()
        return status,net,reversals,fp

    def _document_capacity(self,reversals):
        return self.source_currency_id.round(
            self.credit_move_id.amount_untaxed-sum(reversals.mapped('amount_untaxed')))

    def _approval(self):
        cycle,row,bill=approved_basis(self.claim_line_id)
        if self.credit_move_id.reversed_entry_id and self.credit_move_id.reversed_entry_id!=bill:
            raise ValidationError('The credit reverses a different original accounting document.')
        if self.purpose!='shortage':
            raise ValidationError('Only a documented shortage-resolution credit is eligible.')
        evidence=self.evidence_id
        if not self.credit_line_id.purchase_line_id or not self.credit_line_id.product_id:
            if (not evidence or evidence.effective_state!='verified'
                    or evidence.evidence_type not in ('supplier','supplier_confirmation','supplier_response','credit','account_note')):
                raise ValidationError('Manual attribution requires independently verified evidence for this credit and claim line.')
        return cycle,row,bill

    def _fx(self,target):
        return dated_fx(self.env,self.source_currency_id,target,self.company_id,self.credit_move_id.date)

    def _valuation(self):
        self._match()
        status,net,reversals,fp=self._source()
        if status not in ('posted','partial'):
            raise ValidationError('Use a posted credit line with attributable unreversed capacity.')
        cycle,basis,bill=self._approval()
        credit,currency=self.credit_line_id,self.source_currency_id
        if (abs(currency.round(self.source_amount)-self.source_amount)>1e-9
                or currency.compare_amounts(self.source_amount,net)>0):
            raise ValidationError('The proposal exceeds source capacity or currency precision.')
        tax=credit.price_total-credit.price_subtotal
        if (not math.isfinite(tax) or tax < -1e-9 or credit.amount_currency>=0
                or currency.compare_amounts(abs(credit.amount_currency),credit.price_subtotal)):
            raise ValidationError('The native posted tax and untaxed amounts require reconciliation.')
        correction_lines=reversals.invoice_line_ids.filtered(lambda item: item.display_type=='product'
            and item.product_id==credit.product_id and item.purchase_line_id==credit.purchase_line_id)
        tax-=sum(row.price_total-row.price_subtotal for row in correction_lines)
        if not math.isfinite(tax) or currency.compare_amounts(tax,0)<0:
            raise ValidationError('The corrected credit tax basis requires reconciliation.')
        fx=self._fx(cycle.currency_id)
        amount=currency._convert(self.source_amount,cycle.currency_id,self.company_id,self.credit_move_id.date)
        uom=self.env['uom.uom'].browse(basis['uom_id'])
        qty=0
        if self.source_quantity:
            source_uom=credit.product_uom_id
            if (reversals or not credit.product_id or not source_uom or not source_uom._has_common_reference(uom)
                    or source_uom.compare(self.source_quantity,credit.quantity)>0 or source_uom.compare(credit.quantity,0)<=0
                    or abs(source_uom.round(self.source_quantity)-self.source_quantity)>1e-9
                    or currency.compare_amounts(self.source_amount,credit.price_subtotal*self.source_quantity/credit.quantity)):
                raise ValidationError('Quantity credit requires whole-source proportionality and compatible units.')
            qty=source_uom._compute_quantity(self.source_quantity,uom,round=False)
            if (uom.compare(qty,0)<=0 or abs(uom.round(qty)-qty)>1e-9
                    or cycle.currency_id.compare_amounts(amount,basis['amount']*qty/basis['quantity'])):
                raise ValidationError('The credit quantity does not reconcile to the approved unit valuation.')
        if cycle.currency_id.compare_amounts(amount,0)<=0:
            raise ValidationError('The converted claim value rounds to zero.')
        return cycle,basis,bill,net,reversals,fp,fx,amount,qty,tax

    def _lock(self,role):
        self.ensure_one()
        self._role(role)
        self.check_access('write')
        version_credit(self.env,self.credit_move_id.ids)
        lock_records(self.claim_id.purchase_id)
        lock_records(self.claim_id)
        lock_records(self.claim_line_id)
        lock_records(self)
        self.env.invalidate_all()
        self.check_access('write')

    def _transition(self,values,message):
        old=self.state
        super().write(values)
        if values.get('state') in ('verified','reversed'):
            self.invalidate_model(['available_source'],flush=False)
        self._audit(message,old)

    def action_submit(self):
        self._lock('group_claim_user')
        if self.create_uid!=self.env.user or self.state not in ('draft','posting'):
            raise AccessError('Only the allocation maker can submit the draft.')
        self._match()
        if self.credit_move_id.state not in ('draft','posted'):
            raise ValidationError('A canceled credit cannot be submitted.')
        target='accounts' if self.credit_move_id.state=='posted' else 'posting'
        if target!=self.state:
            self._transition({'state':target},'Credit allocation submitted')

    def action_accounts(self):
        self._lock('group_accounts_reviewer')
        if self.state not in ('accounts','posting') or self.create_uid==self.env.user:
            raise AccessError('An independent Accounts reviewer must confirm the source.')
        cycle,_,bill,_,_,fp,fx,_,_,_=self._valuation()
        self._transition({'state':'finance','source_dirty':False,'source_fingerprint':fp,
            'accounts_by':self.env.uid,'approval_id':cycle.id,'original_bill_id':bill.id,
            'currency_id':cycle.currency_id.id,'fx_basis':fx,'conversion_date':self.credit_move_id.date,
            'source_snapshot_currency_id':self.source_currency_id.id,
            'company_currency_id':self.company_id.currency_id.id},
            'Accounts verified purpose, posted source, tax and FX basis')

    def action_verify(self):
        self._lock('group_finance_manager')
        if self.state=='verified' and not self.source_dirty:
            return
        if self.state!='finance' or self.env.user in (self.create_uid | self.accounts_by):
            raise AccessError('A separate Finance reviewer must authorize credit recovery.')
        cycle,basis,_,net,reversals,fp,fx,amount,qty,tax=self._valuation()
        if (self.source_dirty or fp!=self.source_fingerprint or fx!=self.fx_basis or cycle!=self.approval_id or self.company_currency_id!=self.company_id.currency_id
                or self.source_snapshot_currency_id!=self.source_currency_id):
            raise ValidationError('Source, FX or approval changed. Request a new Accounts review.')
        self.flush_model(['state','source_dirty','source_amount','source_tax','company_value','source_quantity',
                          'credit_line_id','claim_line_id'])
        self.env.cr.execute("""
            SELECT coalesce(sum(source_amount),0),coalesce(sum(source_tax),0),coalesce(sum(company_value),0),
                   coalesce(sum(source_quantity),0),coalesce(bool_or(claim_line_id=%s OR source_dirty),false)
            FROM supplier_shortage_credit_allocation WHERE credit_line_id=%s AND state='verified'
        """,[self.claim_line_id.id,self.credit_line_id.id])
        used,tax_used,company_used,qty_used,duplicate=self.env.cr.fetchone()
        currency=self.source_currency_id
        if duplicate or currency.compare_amounts(used+self.source_amount,net)>0:
            raise ValidationError('This credit line has insufficient unallocated capacity or an existing match.')
        self.flush_model(['credit_move_id','source_snapshot_currency_id'])
        self.env.cr.execute("""
            SELECT coalesce(sum(source_amount),0),coalesce(bool_or(source_snapshot_currency_id!=%s),false)
            FROM supplier_shortage_credit_allocation WHERE credit_move_id=%s AND state='verified'
        """,[currency.id,self.credit_move_id.id])
        document_used,document_currency_conflict=self.env.cr.fetchone()
        if (document_currency_conflict
                or currency.compare_amounts(document_used+self.source_amount,self._document_capacity(reversals))>0):
            raise ValidationError('The net vendor credit document has insufficient unallocated capacity.')
        if qty and self.credit_line_id.product_uom_id.compare(qty_used+self.source_quantity,self.credit_line_id.quantity)>0:
            raise ValidationError('The source credit quantity is already allocated.')
        goods=self.env['supplier.shortage.replacement']
        goods.flush_model(['state','claim_line_id','verified_goods_value','allocated_qty','currency_id'])
        self.env.cr.execute("""
            SELECT coalesce(sum(verified_goods_value),0),coalesce(sum(allocated_qty),0),
                   coalesce(bool_or(currency_id!=%s),false)
            FROM supplier_shortage_replacement WHERE claim_line_id=%s AND state='verified'
        """,[cycle.currency_id.id,self.claim_line_id.id])
        goods_value,goods_qty,goods_conflict=self.env.cr.fetchone()
        credits,credit_qty,credit_conflict=credit_hold(self.claim_line_id)
        if (goods_conflict or credit_conflict or cycle.currency_id.compare_amounts(goods_value+credits+amount,basis['amount'])>0
                or self.env['uom.uom'].browse(basis['uom_id']).compare(goods_qty+credit_qty+qty,basis['quantity'])>0):
            raise ValidationError('Combined goods and credit exceed the approved claim capacity.')
        subtotal=self.credit_line_id.price_subtotal
        company=self.company_currency_id
        allocated_tax=currency.round(currency.round(tax*(used+self.source_amount)/net)-tax_used)
        company_value=company.round(company.round(abs(self.credit_line_id.balance)*(used+self.source_amount)/subtotal)-company_used)
        if allocated_tax<0 or company_value<0:
            raise ValidationError('Prior rounding allocations require reconciliation.')
        self._touch_claim()
        self._transition({'state':'verified','active_credit_line_id':self.credit_line_id.id,
            'claim_value':amount,'claim_quantity':qty,'source_tax':allocated_tax,'company_value':company_value,
            'reversal_ids':[fields.Command.set(reversals.ids)],'verified_by':self.env.uid,
            'verified_at':fields.Datetime.now()},'Vendor credit recovery verified')

    def _touch_claim(self):
        self.env.cr.execute('UPDATE supplier_shortage_claim SET write_date=write_date WHERE id=%s',[self.claim_id.id])

    def action_recheck(self):
        self._lock('group_finance_manager')
        if self.state!='finance':
            raise ValidationError('Select an allocation awaiting Finance.')
        self._transition({'state':'accounts'},'Accounts recheck requested')

    def action_reverse(self):
        self._lock('group_finance_manager')
        if self.state=='reversed':
            return
        if self.state!='verified' or self.create_uid==self.env.user or len((self.reason or '').strip())<20:
            raise ValidationError('A verified allocation and documented independent reversal are required.')
        _,_,reversals,_=self._source()
        self._touch_claim()
        self._transition({'state':'reversed','active_credit_line_id':False,'reversed_at':fields.Datetime.now(),
            'reversal_ids':[fields.Command.set(reversals.ids)]},'Credit allocation reversed')

    def action_reject(self):
        self._lock('group_finance_manager')
        if self.state in ('verified','reversed','rejected') or len((self.reason or '').strip())<20:
            raise ValidationError('Document the rejection of an unverified allocation.')
        self._transition({'state':'rejected'},'Credit allocation rejected')

    def _realization(self):
        move=self.credit_move_id
        if move.state!='posted':
            return 'unknown'
        payable=move.line_ids.filtered(lambda item:item.account_id.account_type=='liability_payable')
        payable.check_access('read')
        if not payable:
            return 'unknown'
        applied=bank=False
        corrections=accounting_corrections(move)
        corrections.check_access('read')
        for line in payable:
            matches=line.matched_debit_ids | line.matched_credit_ids
            matches.check_access('read')
            for match in matches:
                other=(match.debit_move_id | match.credit_move_id)-line
                other.check_access('read')
                doc=other.move_id
                doc.check_access('read')
                if (doc.state!='posted' or doc.company_id!=move.company_id or other.account_id!=line.account_id
                        or other.partner_id.commercial_partner_id!=move.commercial_partner_id or doc in corrections):
                    return 'unknown'
                if doc.move_type=='in_invoice':
                    applied=True
                elif doc.statement_line_id and doc.journal_id.type in ('bank','cash'):
                    doc.statement_line_id.check_access('read')
                    if doc.statement_line_id.amount<=0:
                        return 'unknown'
                    bank=True
                elif doc.origin_payment_id:
                    payment=doc.origin_payment_id
                    payment.check_access('read')
                    if (payment.payment_type!='inbound' or not payment.is_matched
                            or payment.journal_id.type not in ('bank','cash')):
                        return 'unknown'
                    liquidity,_,_=payment._seek_for_lines()
                    liquidity.check_access('read')
                    bank_matches=liquidity.matched_debit_ids | liquidity.matched_credit_ids
                    bank_matches.check_access('read')
                    counterparts=(bank_matches.debit_move_id | bank_matches.credit_move_id)-liquidity
                    counterparts.check_access('read')
                    statements=counterparts.move_id.statement_line_id
                    statements.check_access('read')
                    if not statements or any(stmt.amount<=0 or stmt.company_id!=move.company_id for stmt in statements):
                        return 'unknown'
                    bank=True
                else:
                    return 'unknown'
        complete=move.currency_id.is_zero(move.amount_residual)
        if applied and bank:
            return 'mixed'
        if applied:
            return 'payable' if complete else 'part_payable'
        if bank:
            return 'cash' if complete else 'mixed'
        return 'unknown' if complete else 'open'

    @api.depends('state','source_dirty','claim_value','claim_id.state','claim_id.approval_cycle','claim_id.approval_status',
                 'claim_id.replacement_conflict_cycle','credit_move_id.state','credit_move_id.amount_residual',
                 'credit_move_id.reversal_move_ids.state','evidence_id.effective_state')
    @api.depends_context('uid','allowed_company_ids')
    def _position(self):
        documents=self.credit_move_id.ids
        visible=self.search([('credit_move_id','in',documents),('state','=','verified')])
        per_line=visible.grouped('credit_line_id')
        per_document=visible.grouped('credit_move_id')
        self.flush_model(['credit_move_id','state'])
        self.env.cr.execute("""
            SELECT credit_move_id,count(*) FROM supplier_shortage_credit_allocation
            WHERE credit_move_id=ANY(%s) AND state='verified' GROUP BY credit_move_id
        """,[documents])
        coverage=dict(self.env.cr.fetchall())
        for row in self:
            row.source_status='review'
            row.available_source=row.effective_value=0
            row.realization='unknown'
            try:
                status,net,reversals,fp=row._source()
                row.source_status=status
                known=per_document.get(row.credit_move_id,self.browse())
                if (status in ('posted','partial') and coverage.get(row.credit_move_id.id,0)==len(known)
                        and all(item.source_snapshot_currency_id==row.source_currency_id for item in known)):
                    line_used=sum(per_line.get(row.credit_line_id,self.browse()).mapped('source_amount'))
                    document_left=row._document_capacity(reversals)-sum(known.mapped('source_amount'))
                    row.available_source=max(0,min(net-line_used,document_left))
                effective=(row.state=='verified' and not row.source_dirty and fp and fp==row.source_fingerprint
                           and status in ('posted','partial') and row.approval_id.number==row.claim_id.approval_cycle
                           and row.claim_id.state=='under_review' and row.claim_id.approval_status in ('approved','stale')
                           and row.claim_id.replacement_conflict_cycle!=row.approval_id.number
                           and (not row.evidence_id or row.evidence_id.effective_state=='verified'))
                row.effective_value=row.claim_value if effective else 0
                row.realization=row._realization()
            except AccessError:
                continue

    def _audit(self,message,old):
        self.check_access('read')
        audit=self.env['supplier.shortage.claim.audit']
        super(ShortageClaimAudit,audit.sudo().with_context({})).create({
            'claim_id':self.claim_id.id,'company_id':self.company_id.id,'credit_allocation_id':self.id,
            'event_type':'credit_event','actor_id':self.env.uid,'event_datetime':fields.Datetime.now(),
            'change_summary':message,'reason':self.reason or self.note,'valuation_basis':{
                'previous_state':old,'state':self.state,'line':self.claim_line_id.id,'credit':self.credit_move_id.id,
                'credit_line':self.credit_line_id.id,'bill':self.original_bill_id.id,'purpose':self.purpose,
                'source_currency':(self.source_snapshot_currency_id or self.source_currency_id).id,
                'company_currency':self.company_currency_id.id,'source_amount':self.source_amount,'tax':self.source_tax,
                'claim_value':self.claim_value,'claim_currency':self.currency_id.id,'quantity':self.claim_quantity,
                'company_value':self.company_value,'fx':self.fx_basis,'fingerprint':self.source_fingerprint,
                'reversals':self.reversal_ids.ids},
        })


class CreditAudit(models.Model):
    _inherit='supplier.shortage.claim.audit'

    credit_allocation_id=fields.Many2one(MODEL,readonly=True,index=True,ondelete='restrict')
    event_type=fields.Selection(selection_add=[('credit_event','Vendor Credit Settlement')],
                                ondelete={'credit_event':lambda rows:None})

    def _access_domain(self,operation):
        return super()._access_domain(operation) & (
            Domain('credit_allocation_id','=',False) | Domain('credit_allocation_id','access','read'))


class CreditClaimLine(models.Model):
    _inherit='supplier.shortage.claim.line'

    credit_allocation_ids=fields.One2many(MODEL,'claim_line_id',readonly=True,copy=False)
    credit_value=fields.Monetary(compute='_credit_position',aggregator=False)
    reversed_credit_value=fields.Monetary(compute='_credit_position',aggregator=False)
    settlement_approved_value=fields.Monetary(compute='_credit_position',aggregator=False)
    settlement_unresolved_value=fields.Monetary(compute='_credit_position',aggregator=False)
    settlement_review=fields.Boolean(compute='_credit_position')

    @api.depends('credit_allocation_ids.effective_value','credit_allocation_ids.state','replacement_value',
                 'replacement_unresolved_value','claim_id.approval_cycle','cash_settlement_ids.effective_value',
                 'cash_settlement_ids.realized_value','cash_settlement_ids.state')
    @api.depends_context('uid','allowed_company_ids')
    def _credit_position(self):
        model=self.env[MODEL]
        rows=model.search([('claim_line_id','in',self.ids),('state','in',('verified','reversed'))]).grouped(
            'claim_line_id') if model.has_access('read') else {}
        goods=self.env['supplier.shortage.replacement']
        cash=self.env['supplier.shortage.cash.settlement']
        cash_rows=cash.search([('claim_line_id','in',self.ids),('state','=','verified')]).grouped('claim_line_id') if cash.has_access('read') else {}
        cash.flush_model(['claim_line_id','state'])
        model.flush_model(['claim_line_id','state'])
        goods.flush_model(['claim_line_id','state'])
        self.env.cr.execute("""
            SELECT claim_line_id,count(*) FROM (
                SELECT claim_line_id FROM supplier_shortage_credit_allocation WHERE state='verified'
                  AND claim_line_id=ANY(%s)
                UNION ALL SELECT claim_line_id FROM supplier_shortage_replacement WHERE state='verified'
                  AND claim_line_id=ANY(%s)
                UNION ALL SELECT claim_line_id FROM supplier_shortage_cash_settlement WHERE state='verified'
                  AND claim_line_id=ANY(%s)
            ) held GROUP BY claim_line_id
        """,[self.ids,self.ids,self.ids])
        coverage=dict(self.env.cr.fetchall())
        visible_goods=goods.search([('claim_line_id','in',self.ids),('state','=','verified')]).grouped(
            'claim_line_id') if goods.has_access('read') else {}
        approvals=self.env['supplier.shortage.approval.cycle']
        cycles=approvals.search([('claim_id','in',self.claim_id.ids),('state','=','approved')]) if approvals.has_access('read') else approvals
        current={cycle.claim_id.id:cycle for cycle in cycles if cycle.number==cycle.claim_id.approval_cycle}
        frozen={item['line_id']:item for cycle in current.values()
                for item in cycle.snapshot.get('basis',{}).get('financial_lines',[])}
        for line in self:
            allocations=rows.get(line,model.browse())
            active=allocations.filtered(lambda row:row.state=='verified')
            goods_rows=visible_goods.get(line,goods.browse())
            line.credit_value=sum(active.filtered(lambda row:row.currency_id==line.currency_id).mapped('effective_value'))
            line.reversed_credit_value=sum(allocations.filtered(
                lambda row:row.state=='reversed' and row.currency_id==line.currency_id).mapped('claim_value'))
            basis=frozen.get(line.id,{})
            line.settlement_approved_value=basis.get('amount',0)
            cash_line=cash_rows.get(line,cash.browse())
            eligible=cash_line.filtered(lambda row:row.currency_id==line.currency_id)
            line.cash_value=sum(eligible.filtered(lambda row:row.kind=='direct').mapped('effective_value'))
            line.cash_realization=sum(eligible.filtered(lambda row:row.kind=='credit').mapped('realized_value'))
            line.payable_realization=sum(eligible.filtered(lambda row:row.kind=='offset').mapped('realized_value'))
            incomplete=coverage.get(line.id,0)!=len(active)+len(goods_rows)+len(cash_line)
            unsettled=line.settlement_approved_value-line.replacement_value-line.credit_value-line.cash_value
            currency=line.currency_id
            line.settlement_review=(incomplete or (currency and currency.compare_amounts(unsettled,0)<0)
                                    or any(not row.effective_value for row in active)
                                    or any(not row.effective_qty for row in goods_rows)
                                    or any(not row.realized_value for row in cash_line))
            line.settlement_unresolved_value=currency.round(unsettled) if currency and not incomplete else 0


class CreditClaim(models.Model):
    _inherit='supplier.shortage.claim'

    credit_allocation_ids=fields.One2many(MODEL,'claim_id',readonly=True,copy=False)
    credit_summary_line_ids=fields.One2many('supplier.shortage.claim.line','claim_id',readonly=True,copy=False)

    @api.model_create_multi
    def create(self, values):
        outputs={'credit_summary_line_ids','credit_allocation_ids'}
        if any(outputs & vals.keys() for vals in values) or any('default_'+key in self.env.context for key in outputs):
            raise AccessError('Open the credit workbench to manage allocations.')
        return super().create(values)

    def write(self, values):
        if {'credit_summary_line_ids','credit_allocation_ids'} & values.keys():
            raise AccessError('Open the credit workbench to manage allocations.')
        return super().write(values)

    @api.depends('replacement_ids.effective_value','supplier_position_currency_id','replacement_conflict_cycle',
                 'credit_allocation_ids.effective_value')
    @api.depends_context('uid','allowed_company_ids')
    def _compute_verified_recovery(self):
        super()._compute_verified_recovery()
        model=self.env[MODEL]
        if not model.has_access('read'):
            return
        grouped=model.search([('claim_id','in',self.ids),('state','=','verified')]).grouped('claim_id')
        for claim in self:
            allocations=grouped.get(claim,model.browse())
            claim.verified_recovery_amount+=sum(allocations.filtered(
                lambda row:row.currency_id==claim.supplier_position_currency_id).mapped('effective_value'))

    def action_open_credits(self):
        self.ensure_one()
        self.check_access('read')
        self.env[MODEL].check_access('read')
        action=self.env['ir.actions.act_window']._for_xml_id(ROLE+'action_credit_allocation')
        action['domain']=[('claim_id','=',self.id)]
        return action


class CreditMatching(models.TransientModel):
    _name='supplier.shortage.credit.matching'
    _description='Allocate Vendor Credit Note'

    claim_line_id=fields.Many2one('supplier.shortage.claim.line',required=True)
    supplier_id=fields.Many2one(related='claim_line_id.claim_id.partner_id',compute_sudo=False)
    company_id=fields.Many2one(related='claim_line_id.company_id',compute_sudo=False)
    credit_line_id=fields.Many2one('account.move.line',required=True)
    purpose=fields.Selection(PURPOSE,required=True,default='review')
    source_currency_id=fields.Many2one(related='credit_line_id.currency_id',compute_sudo=False)
    supplier_commercial_id=fields.Many2one(related='supplier_id.commercial_partner_id',compute_sudo=False)
    source_state=fields.Selection(related='credit_line_id.move_id.state',compute_sudo=False)
    claim_currency_id=fields.Many2one(related='claim_line_id.currency_id',compute_sudo=False)
    approved_value=fields.Monetary(related='claim_line_id.settlement_approved_value',currency_field='claim_currency_id',compute_sudo=False)
    goods_value=fields.Monetary(related='claim_line_id.replacement_value',currency_field='claim_currency_id',compute_sudo=False)
    allocated_credit=fields.Monetary(related='claim_line_id.credit_value',currency_field='claim_currency_id',compute_sudo=False)
    unresolved_value=fields.Monetary(related='claim_line_id.settlement_unresolved_value',currency_field='claim_currency_id',compute_sudo=False)
    settlement_review=fields.Boolean(related='claim_line_id.settlement_review',compute_sudo=False)
    source_subtotal=fields.Monetary(related='credit_line_id.price_subtotal',currency_field='source_currency_id',compute_sudo=False)
    source_amount=fields.Monetary(required=True,currency_field='source_currency_id')
    source_quantity=fields.Float(digits='Product Unit')
    evidence_id=fields.Many2one('supplier.shortage.claim.evidence')
    note=fields.Text(required=True)

    available_source=fields.Monetary(compute='_source_preview',currency_field='source_currency_id')
    source_tax_total=fields.Monetary(compute='_source_preview',currency_field='source_currency_id')
    conversion_date=fields.Date(related='credit_line_id.move_id.date',compute_sudo=False)

    @api.depends('claim_line_id','credit_line_id','credit_line_id.move_id.state','credit_line_id.price_subtotal')
    @api.depends_context('uid','allowed_company_ids')
    def _source_preview(self):
        model=self.env[MODEL]
        for wizard in self:
            wizard.available_source=wizard.source_tax_total=0
            try:
                line,source=wizard.claim_line_id,wizard.credit_line_id
                check_credit_identity(line,source)
                projection=model.new({'claim_line_id':line.id,'credit_line_id':source.id})
                wizard.available_source=projection.available_source
                wizard.source_tax_total=source.price_total-source.price_subtotal
            except (AccessError,ValidationError):
                continue

    @api.model_create_multi
    def create(self, values):
        rows=super().create(values)
        for wizard in rows:
            check_credit_identity(wizard.claim_line_id,wizard.credit_line_id)
        return rows

    def write(self, values):
        result=super().write(values)
        for wizard in self:
            check_credit_identity(wizard.claim_line_id,wizard.credit_line_id)
        return result

    def action_propose(self):
        self.ensure_one()
        self.check_access('read')
        self.claim_line_id.check_access('read')
        self.credit_line_id.check_access('read')
        row=self.env[MODEL].create({
            'claim_line_id':self.claim_line_id.id,'credit_line_id':self.credit_line_id.id,'purpose':self.purpose,
            'source_amount':self.source_amount,'source_quantity':self.source_quantity,
            'evidence_id':self.evidence_id.id,'note':self.note,
        })
        return {'type':'ir.actions.act_window','res_model':MODEL,'res_id':row.id,'views':[(False,'form')]}

class CreditCurrencyRate(models.Model):
    _inherit='res.currency.rate'

    def _flag_credit_rate_change(self):
        if not self.env.registry.ready or not self:
            return
        self.env[MODEL].flush_model(['fx_basis','state','credit_move_id'])
        self.env.cr.execute("""
            SELECT DISTINCT credit_move_id FROM supplier_shortage_credit_allocation allocation
            WHERE allocation.state IN ('finance','verified') AND EXISTS (
                SELECT 1 FROM unnest(%s::integer[]) rate(id)
                WHERE allocation.fx_basis @> jsonb_build_object('rates',jsonb_build_array(jsonb_build_object('id',rate.id)))
            )
        """,[self.ids])
        # Only source identities are propagated, even for a rate editor without
        # access to the underlying accounting records.
        _mark_credit_roots(self.env,[row[0] for row in self.env.cr.fetchall() if row[0]])

    def write(self, values):
        if values.keys() & {'rate','company_rate','inverse_company_rate','currency_id','company_id','name'}:
            self._flag_credit_rate_change()
        return super().write(values)

    def unlink(self):
        self._flag_credit_rate_change()
        return super().unlink()
