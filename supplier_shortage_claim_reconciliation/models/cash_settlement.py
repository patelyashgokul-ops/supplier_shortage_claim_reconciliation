"""Read-only allocations of native supplier bank and payable evidence."""
import hashlib
import json
import math

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain

from .claim_preparation import ROLE, lock_records
from .credit_settlement import accounting_corrections, approved_basis, dated_fx, financial_hold, version_credit
from .shortage_claim_audit import ShortageClaimAudit

MODEL = 'supplier.shortage.cash.settlement'
KINDS = [('review','Unclassified receipt'),('direct','Direct claim refund'),
         ('credit','Credit-linked refund'),('offset','Credit payable offset'),('unrelated','Unrelated receipt')]
INPUTS = {'claim_line_id','source_line_id','bank_id','payment_id','credit_id','kind','gross_amount',
          'untaxed_amount','evidence_id','note','reason'}


def cash_hold(line):
    line.env[MODEL].flush_model(['claim_line_id','state','kind','claim_value','currency_id'])
    line.env.cr.execute("""
        SELECT coalesce(sum(claim_value),0),coalesce(bool_or(currency_id!=%s),false)
        FROM supplier_shortage_cash_settlement WHERE claim_line_id=%s AND state='verified' AND kind='direct'
    """,[line.currency_id.id,line.id])
    return line.env.cr.fetchone()


def version_cash(env, companies):
    if companies:
        env.cr.execute("""
            INSERT INTO ssc_cash_company_version(company_id) SELECT unnest(%s::integer[])
            ON CONFLICT(company_id) DO UPDATE SET company_id=excluded.company_id
        """,[sorted(set(companies))])


def cash_source_changed(env, moves, companies=()):
    if not env.registry.ready or not (moves or companies):
        return
    relevant=moves.filtered(lambda move: move.statement_line_id or move.origin_payment_id
        or move.move_type in ('in_invoice','in_refund') or move.reversed_entry_id)
    roots=relevant | relevant.reversed_entry_id
    if not roots and not companies:
        return
    _mark_cash_sources(env,roots.ids,list(companies)+roots.company_id.ids)


def _mark_cash_sources(env,ids,companies):
    version_cash(env,companies)
    if not ids:
        return
    env[MODEL].flush_model(['source_move_id','bank_move_id','credit_move_id','state','source_dirty'])
    env.cr.execute("""
        WITH changed AS (
            UPDATE supplier_shortage_cash_settlement SET source_dirty=true
            WHERE state IN ('finance','verified') AND NOT source_dirty
              AND (source_move_id=ANY(%s) OR bank_move_id=ANY(%s) OR credit_move_id=ANY(%s))
            RETURNING id,claim_id,company_id,source_fingerprint
        )
        INSERT INTO supplier_shortage_claim_audit
            (claim_id,company_id,cash_settlement_id,event_type,actor_id,event_datetime,change_summary,valuation_basis)
        SELECT claim_id,company_id,id,'cash_event',%s,statement_timestamp() AT TIME ZONE 'UTC',
               'Cash or payable source requires revalidation',json_build_object('fingerprint',source_fingerprint)
        FROM changed
    """,[ids,ids,ids,env.uid])
    env[MODEL].invalidate_model(['source_dirty','effective_value','realized_value','source_status'],flush=False)
    env['supplier.shortage.claim.line'].invalidate_model(
        ['cash_value','cash_realization','payable_realization','settlement_unresolved_value','settlement_review'],flush=False)
    env['supplier.shortage.claim'].invalidate_model(['verified_recovery_amount','audit_event_ids'],flush=False)


def cash_rates_changed(env,rate_ids):
    if not env.registry.ready or not rate_ids:
        return
    env[MODEL].flush_model(['fx_basis','kind','state','source_move_id','company_id'])
    env.cr.execute("""
        SELECT DISTINCT source_move_id,company_id FROM supplier_shortage_cash_settlement allocation
        WHERE allocation.kind='direct' AND allocation.state IN ('finance','verified') AND EXISTS (
            SELECT 1 FROM unnest(%s::integer[]) rate(id)
            WHERE allocation.fx_basis @> jsonb_build_object('rates',jsonb_build_array(jsonb_build_object('id',rate.id)))
        )
    """,[rate_ids])
    rows=env.cr.fetchall()
    _mark_cash_sources(env,[move for move,_company in rows if move],[company for _move,company in rows if company])


class CashSettlement(models.Model):
    _name=MODEL
    _description='Supplier Cash and Payable Settlement'
    _rec_name='claim_id'
    _order='id desc'
    _check_company_auto=True

    claim_line_id=fields.Many2one('supplier.shortage.claim.line',required=True,check_company=True,ondelete='restrict',index=True)
    claim_id=fields.Many2one(related='claim_line_id.claim_id',store=True,index=True)
    company_id=fields.Many2one(related='claim_line_id.company_id',store=True,index=True)
    supplier_id=fields.Many2one(related='claim_id.partner_id')
    source_line_id=fields.Many2one('account.move.line',check_company=True,ondelete='set null',index=True)
    source_move_id=fields.Many2one(related='source_line_id.move_id',store=True,index=True,compute_sudo=False)
    bank_id=fields.Many2one('account.bank.statement.line',check_company=True,ondelete='set null',index=True)
    bank_move_id=fields.Many2one(related='bank_id.move_id',store=True,index=True,compute_sudo=False)
    payment_id=fields.Many2one('account.payment',check_company=True,ondelete='set null',index=True)
    credit_id=fields.Many2one('supplier.shortage.credit.allocation',check_company=True,ondelete='restrict',index=True)
    credit_move_id=fields.Many2one(related='credit_id.credit_move_id',store=True,index=True,compute_sudo=False)
    partial_id=fields.Many2one('account.partial.reconcile',readonly=True,ondelete='set null',index=True,copy=False)
    kind=fields.Selection(KINDS,required=True,default='review')
    source_currency_id=fields.Many2one(related='source_line_id.currency_id',compute_sudo=False)
    currency_id=fields.Many2one('res.currency',readonly=True,copy=False)
    frozen_source_currency_id=fields.Many2one('res.currency',readonly=True,copy=False)
    company_currency_id=fields.Many2one('res.currency',readonly=True,copy=False)
    company_amount=fields.Monetary(currency_field='company_currency_id',readonly=True,copy=False,aggregator=False)
    gross_amount=fields.Monetary(currency_field='source_currency_id',required=True,aggregator=False)
    untaxed_amount=fields.Monetary(currency_field='source_currency_id',aggregator=False)
    claim_value=fields.Monetary(readonly=True,aggregator=False,copy=False)
    realized_snapshot=fields.Monetary(readonly=True,aggregator=False,copy=False)
    effective_value=fields.Monetary(compute='_position',aggregator=False)
    realized_value=fields.Monetary(compute='_position',aggregator=False)
    source_status=fields.Selection([('review','Requires review'),('recorded','Payment recorded'),
        ('posted','Accounting posted'),('transit','Payment awaiting bank match'),('available','Bank evidence available'),
        ('bank','Bank confirmed'),('offset','Payable offset confirmed')],compute='_position')
    evidence_id=fields.Many2one('supplier.shortage.claim.evidence',check_company=True,ondelete='restrict')
    note=fields.Text(required=True)
    reason=fields.Text()
    state=fields.Selection([('draft','Draft'),('accounts','Accounts review'),('finance','Finance review'),
        ('verified','Verified'),('reversed','Reversed'),('rejected','Rejected')],default='draft',required=True,readonly=True,copy=False,index=True)
    source_dirty=fields.Boolean(readonly=True,copy=False)
    source_fingerprint=fields.Char(readonly=True,copy=False)
    approval_id=fields.Many2one('supplier.shortage.approval.cycle',readonly=True,ondelete='restrict',copy=False)
    accounting_date=fields.Date(readonly=True,copy=False)
    bank_date=fields.Date(readonly=True,copy=False)
    fx_basis=fields.Json(readonly=True,copy=False)
    accounts_by=fields.Many2one('res.users',readonly=True,copy=False)
    verified_by=fields.Many2one('res.users',readonly=True,copy=False)
    verified_at=fields.Datetime(readonly=True,copy=False)
    reversed_at=fields.Datetime(readonly=True,copy=False)
    reversal_ids=fields.Many2many('account.move',readonly=True,copy=False)
    audit_ids=fields.One2many('supplier.shortage.claim.audit','cash_settlement_id',readonly=True,copy=False)

    def init(self):
        self.env.cr.execute('CREATE TABLE IF NOT EXISTS ssc_cash_company_version(company_id integer PRIMARY KEY)')

    def _access_domain(self,operation):
        domain=super()._access_domain(operation) & Domain('claim_line_id','access','read')
        if not self.env['account.move.line'].has_access('read'):
            return Domain.FALSE
        for name in ('source_line_id','source_move_id','bank_id','payment_id','credit_id','evidence_id','partial_id'):
            domain &= Domain(name,'=',False) | Domain(name,'access','read')
        return domain & ~Domain('reversal_ids','any!',~Domain('id','access','read'))

    def _role(self,role):
        if self.env.su or not self.env.user.has_group(ROLE+role) or any(row.company_id not in self.env.companies for row in self):
            raise AccessError('Use a designated settlement role in the active claim company.')

    def _match(self):
        self.ensure_one()
        self.check_access('read')
        line,move=self.source_line_id,self.source_move_id
        if not line or not move or move.company_id!=self.company_id:
            raise ValidationError('Select an existing source in the claim company.')
        partner=self.supplier_id.commercial_partner_id
        if not partner or line.partner_id.commercial_partner_id!=partner:
            raise ValidationError('The accounting line must identify the claim supplier.')
        if (len((self.note or '').strip())<20 or not all(math.isfinite(n) for n in (self.gross_amount,self.untaxed_amount))
                or self.gross_amount<=0 or self.untaxed_amount<0 or self.untaxed_amount>self.gross_amount):
            raise ValidationError('Document the match and enter valid gross and untaxed amounts.')
        if self.credit_id and self.credit_id.claim_line_id!=self.claim_line_id:
            raise ValidationError('The realization must use a credit allocation for the same claim line.')
        if self.bank_id and self.bank_id.company_id!=self.company_id:
            raise ValidationError('Select bank evidence in the claim company.')
        if self.payment_id and self.payment_id.move_id!=move:
            raise ValidationError('The payment must own the selected accounting line.')
        if self.evidence_id and (self.evidence_id.claim_id!=self.claim_id
                                 or self.claim_line_id not in self.evidence_id._scope_lines()):
            raise ValidationError('Evidence must cover the selected claim line.')

    @api.model_create_multi
    def create(self,values):
        self._role('group_claim_user')
        if any(vals.keys()-INPUTS for vals in values):
            raise AccessError('Cash decisions and snapshots are system controlled.')
        rows=super(CashSettlement,self.with_context({},allowed_company_ids=self.env.companies.ids)).create(values)
        for row in rows:
            row._match()
            row._audit('Cash settlement proposed',False)
        return rows.with_env(self.env)

    def write(self,values):
        self.check_access('write')
        if values.keys()-INPUTS:
            raise AccessError('Use the recovery workflow actions.')
        lock_records(self)
        self.invalidate_recordset()
        for row in self:
            if values.keys()<={'reason'} and row.state not in ('reversed','rejected'):
                continue
            row._role('group_claim_user')
            if row.state!='draft' or row.create_uid!=self.env.user:
                raise AccessError('Only the maker can edit a draft.')
        result=super().write(values)
        if values.keys()-{'reason'}:
            for row in self:
                row._match()
                row._audit('Cash proposal updated',row.state)
        return result

    def unlink(self):
        raise AccessError('Settlement history is retained.')

    def _evidence(self):
        self._match()
        line,move,bank,payment=self.source_line_id,self.source_move_id,self.bank_id,self.payment_id
        currency=line.currency_id
        corrections=accounting_corrections(move | bank.move_id)
        corrections.check_access('read')
        if (move.state!='posted' or move.reversed_entry_id or corrections.filtered(lambda item:item.state=='posted')
                or not currency or line.balance>=0 or line.amount_currency>=0
                or not move.name or move.name=='/'):
            raise ValidationError('Select a posted supplier credit-side item with no active accounting reversal.')
        if self.kind not in ('direct','credit','offset'):
            raise ValidationError('Classify the receipt before requesting financial verification.')
        partner=self.supplier_id.commercial_partner_id
        bank_amount=0
        bank_match=self.env['account.partial.reconcile']
        if self.kind=='offset':
            if bank or payment or move.move_type!='in_invoice':
                raise ValidationError('Select the existing payable on a vendor bill for an offset.')
        else:
            if (not bank or bank.move_id.state!='posted' or bank.move_id.reversed_entry_id
                    or bank.journal_id.type not in ('bank','cash') or bank.amount<=0 or not bank.is_reconciled
                    or bank.partner_id.commercial_partner_id!=partner):
                raise ValidationError('Bank confirmation requires matching posted inbound statement evidence.')
            bank.move_id.check_access('read')
            bank.move_id.line_ids.check_access('read')
            liquidity,suspense,counterparts=bank._seek_for_lines()
            if not liquidity or sum(liquidity.mapped('balance'))<=0 or suspense:
                raise ValidationError('The bank statement needs a resolved liquidity and counterpart match.')
            transaction_currency=bank.foreign_currency_id or bank.currency_id
            bank_amount=bank.amount_currency if bank.foreign_currency_id else bank.amount
            if currency!=transaction_currency or bank_amount<=0:
                raise ValidationError('The statement and source item need a common, traceable transaction currency.')
            if payment:
                payment.move_id.line_ids.check_access('read')
                if (self.kind!='credit' or move.origin_payment_id!=payment or payment.payment_type!='inbound'
                        or payment.partner_type!='supplier' or payment.state not in ('paid','reconciled')
                        or payment.partner_id.commercial_partner_id!=partner or payment.journal_id.type not in ('bank','cash')):
                    raise ValidationError('Use an inbound supplier payment with posted bank evidence.')
                cash_legs,payables,writeoffs=payment._seek_for_lines()
                if len(cash_legs)!=1 or line not in payables or writeoffs or not cash_legs.currency_id.is_zero(cash_legs.amount_residual_currency):
                    raise ValidationError('The payment requires a fully reconciled, unambiguous bank leg.')
                bank_match=cash_legs.matched_credit_ids
                bank_match.check_access('read')
                bank_match.credit_move_id.check_access('read')
                if (len(bank_match)!=1 or bank_match.credit_move_id not in counterparts
                        or cash_legs.currency_id!=currency
                        or currency.compare_amounts(bank_match.debit_amount_currency,abs(cash_legs.amount_currency))
                        or currency.compare_amounts(bank_match.credit_amount_currency,bank_amount)):
                    raise ValidationError('The payment cannot be fully traced to the selected bank transaction.')
            elif move!=bank.move_id or line not in counterparts:
                raise ValidationError('The selected item must be a counterpart on the native bank transaction.')

        partial=self.env['account.partial.reconcile']
        cap=abs(line.amount_currency)
        credit=self.credit_id
        if self.kind=='direct':
            evidence=self.evidence_id
            if (credit or payment or len(counterparts)!=1 or line.account_id.reconcile
                    or line.account_id.account_type not in ('income_other','income','expense','expense_direct_cost')
                    or line.matched_debit_ids or line.matched_credit_ids or line.tax_ids or line.tax_line_id):
                raise ValidationError('A direct refund requires an independent, untaxed non-payable bank counterpart.')
            if (not evidence or evidence.effective_state!='verified' or evidence.evidence_type not in
                    ('supplier','supplier_confirmation','supplier_response','account_note')):
                raise ValidationError('Direct cash requires independently verified claim-specific supplier evidence.')
            if self.untaxed_amount<=0:
                raise ValidationError('Enter the documented untaxed claim amount.')
        else:
            if self.untaxed_amount or not credit or not credit.effective_value or credit.source_snapshot_currency_id!=currency:
                raise ValidationError('Realization requires an effective credit allocation in the source currency.')
            if line.account_id.account_type!='liability_payable':
                raise ValidationError('Credit realization must use the reconciled supplier payable item.')
            matches=line.matched_debit_ids
            matches.check_access('read')
            matches.debit_move_id.check_access('read')
            partial=matches.filtered(lambda item:item.debit_move_id.move_id==credit.credit_move_id)
            if (len(partial)!=1 or partial.debit_move_id.account_id!=line.account_id
                    or partial.debit_move_id.partner_id.commercial_partner_id!=partner
                    or partial.debit_move_id.currency_id!=currency):
                raise ValidationError('A unique native payable match to this credit is required.')
            cap=min(cap,partial.credit_amount_currency,partial.debit_amount_currency)
        if bank:
            cap=min(cap,bank_amount)
        if (currency.compare_amounts(cap,0)<=0 or currency.compare_amounts(self.gross_amount,cap)>0
                or abs(currency.round(self.gross_amount)-self.gross_amount)>1e-9
                or abs(currency.round(self.untaxed_amount)-self.untaxed_amount)>1e-9):
            raise ValidationError('The proposal exceeds the verified source amount or currency precision.')
        signature=[move.id,str(move.date),line.id,line.balance,line.amount_currency,line.account_id.id,partner.id,
                   currency.id,currency.rounding,bank.id,str(bank.date),bank_amount,payment.id,
                   bank_match.id,partial.id,partial.amount,partial.debit_amount_currency,
                   partial.credit_amount_currency,credit.id,credit.source_fingerprint,self.kind]
        return hashlib.sha256(json.dumps(signature).encode()).hexdigest(),partial,cap,bank_amount

    def _valuation(self):
        fingerprint,partial,cap,bank_amount=self._evidence()
        cycle,basis,_=approved_basis(self.claim_line_id)
        source=self.source_currency_id
        date=self.bank_id.date or self.source_move_id.date
        if self.kind=='direct':
            fx=dated_fx(self.env,source,cycle.currency_id,self.company_id,date)
            claim_value=source._convert(self.untaxed_amount,cycle.currency_id,self.company_id,date)
            if cycle.currency_id.is_zero(claim_value):
                raise ValidationError('The normalized claim value rounds to zero.')
        else:
            fx=self.credit_id.fx_basis
            claim_value=0
        return cycle,basis,fingerprint,partial,cap,bank_amount,fx,claim_value

    def _lock(self,role):
        self.ensure_one()
        self._role(role)
        self.check_access('write')
        version_cash(self.env,self.company_id.ids)
        version_credit(self.env,self.credit_move_id.ids)
        lock_records(self.claim_id.purchase_id)
        lock_records(self.claim_id)
        lock_records(self.claim_line_id)
        lock_records(self)
        self.env.invalidate_all()
        self.check_access('write')

    def _transition(self,values,text):
        before=self.state
        super().write(values)
        self._audit(text,before)

    def action_submit(self):
        self._lock('group_claim_user')
        if self.state!='draft' or self.create_uid!=self.env.user:
            raise AccessError('Only the maker can submit a draft.')
        self._match()
        self._transition({'state':'accounts'},'Cash settlement submitted')

    def action_accounts(self):
        self._lock('group_accounts_reviewer')
        if self.state!='accounts' or self.create_uid==self.env.user:
            raise AccessError('An independent Accounts reviewer is required.')
        cycle,_,fp,partial,_,_,fx,_=self._valuation()
        self._transition({'state':'finance','source_dirty':False,'source_fingerprint':fp,'partial_id':partial.id,
            'accounts_by':self.env.uid,'approval_id':cycle.id,'currency_id':cycle.currency_id.id,
            'frozen_source_currency_id':self.source_currency_id.id,'fx_basis':fx,'accounting_date':self.source_move_id.date,
            'company_currency_id':self.company_id.currency_id.id,
            'company_amount':self.company_id.currency_id.round(
                abs(self.source_line_id.balance*self.gross_amount/self.source_line_id.amount_currency)),
            'bank_date':self.bank_id.date},'Accounts confirmed source, classification and valuation')

    def action_verify(self):
        self._lock('group_finance_manager')
        if self.state=='verified' and not self.source_dirty:
            return
        if self.state!='finance' or self.env.user in (self.create_uid | self.accounts_by):
            raise AccessError('A separate Finance reviewer must authorize this settlement.')
        cycle,basis,fp,partial,cap,bank_amount,fx,claim_value=self._valuation()
        if (self.source_dirty or self.source_fingerprint!=fp or self.approval_id!=cycle or self.fx_basis!=fx
                or self.frozen_source_currency_id!=self.source_currency_id
                or self.company_currency_id!=self.company_id.currency_id):
            raise ValidationError('The source or approval changed. Request an Accounts recheck.')
        self.flush_model(['state','gross_amount','realized_snapshot','source_line_id','bank_id','credit_id','partial_id',
                          'claim_line_id','frozen_source_currency_id'])
        self.env.cr.execute("""
            SELECT coalesce(sum(gross_amount) FILTER (WHERE source_line_id=%s),0),
                   coalesce(sum(gross_amount) FILTER (WHERE bank_id=%s),0),
                   coalesce(sum(gross_amount) FILTER (WHERE partial_id=%s),0),
                   coalesce(sum(gross_amount) FILTER (WHERE credit_id=%s),0),
                   coalesce(sum(realized_snapshot) FILTER (WHERE credit_id=%s),0),
                   coalesce(bool_or(source_line_id=%s AND claim_line_id=%s AND credit_id IS NOT DISTINCT FROM %s),false),
                   coalesce(bool_or(frozen_source_currency_id!=%s),false)
            FROM supplier_shortage_cash_settlement WHERE state='verified'
              AND (source_line_id=%s OR bank_id=%s OR credit_id=%s OR partial_id=%s)
        """,[self.source_line_id.id,self.bank_id.id or None,partial.id or None,self.credit_id.id or None,
              self.credit_id.id or None,self.source_line_id.id,self.claim_line_id.id,self.credit_id.id or None,
              self.source_currency_id.id,self.source_line_id.id,self.bank_id.id or None,self.credit_id.id or None,partial.id or None])
        line_used,bank_used,matched_used,credit_used,realized,duplicate,currency_conflict=self.env.cr.fetchone()
        source=self.source_currency_id
        gross=self.gross_amount
        if (duplicate or currency_conflict
                or source.compare_amounts(line_used+gross,abs(self.source_line_id.amount_currency))>0
                or (self.bank_id and source.compare_amounts(bank_used+gross,bank_amount)>0)
                or (partial and source.compare_amounts(matched_used+gross,cap)>0)):
            raise ValidationError('The accounting source has insufficient unallocated capacity or an existing match.')
        currency=cycle.currency_id
        if self.kind=='direct':
            goods=self.env['supplier.shortage.replacement']
            goods.flush_model(['state','claim_line_id','verified_goods_value','currency_id'])
            self.env.cr.execute("""
                SELECT coalesce(sum(verified_goods_value),0),coalesce(bool_or(currency_id!=%s),false)
                FROM supplier_shortage_replacement WHERE claim_line_id=%s AND state='verified'
            """,[currency.id,self.claim_line_id.id])
            goods_value,wrong_currency=self.env.cr.fetchone()
            financial,_,conflict=financial_hold(self.claim_line_id)
            if wrong_currency or conflict or currency.compare_amounts(goods_value+financial+claim_value,basis['amount'])>0:
                raise ValidationError('Combined goods, credit and direct cash exceed the approved claim capacity.')
            realized_value=claim_value
        else:
            credit=self.credit_id
            credit_gross=credit.source_amount+credit.source_tax
            if source.compare_amounts(credit_used+gross,credit_gross)>0:
                raise ValidationError('The credit allocation has insufficient remaining realization capacity.')
            realized_value=currency.round(currency.round(credit.claim_value*(credit_used+gross)/credit_gross)-realized)
            if realized_value<0:
                raise ValidationError('Historical realization rounding requires review.')
        self._touch_claim()
        self._transition({'state':'verified','claim_value':claim_value,'realized_snapshot':realized_value,
            'verified_by':self.env.uid,'verified_at':fields.Datetime.now()},'Cash or payable settlement verified')

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
        if self.state!='verified' or len((self.reason or '').strip())<20:
            raise ValidationError('Document an independent reversal of the verified allocation.')
        reversals=accounting_corrections(self.source_move_id | self.bank_move_id)
        reversals.check_access('read')
        self._touch_claim()
        self._transition({'state':'reversed','reversed_at':fields.Datetime.now(),
                          'reversal_ids':[fields.Command.set(reversals.ids)]},'Cash allocation reversed')

    def action_reject(self):
        self._lock('group_finance_manager')
        if self.state in ('verified','reversed','rejected') or len((self.reason or '').strip())<20:
            raise ValidationError('Document the rejection of an unverified allocation.')
        self._transition({'state':'rejected'},'Cash allocation rejected')

    @api.depends('state','source_dirty','claim_value','realized_snapshot','claim_id.state','claim_id.approval_status',
                 'claim_id.approval_cycle','claim_id.replacement_conflict_cycle','credit_id.effective_value',
                 'source_move_id.state','bank_id.is_reconciled','payment_id.state','partial_id','evidence_id.effective_state')
    @api.depends_context('uid','allowed_company_ids')
    def _position(self):
        for row in self:
            row.source_status='review'
            row.effective_value=row.realized_value=0
            try:
                if row.payment_id:
                    row.source_status='transit' if row.source_move_id.state=='posted' else 'recorded'
                elif row.source_move_id.state=='posted':
                    row.source_status='posted'
                if row.bank_id and row.bank_id.move_id.state=='posted':
                    row.source_status='available'
                fingerprint,_,_,_=row._evidence()
                row.source_status='offset' if row.kind=='offset' else 'bank'
                claim=row.claim_id
                if (row.state=='verified' and not row.source_dirty and fingerprint==row.source_fingerprint
                        and claim.state=='under_review' and claim.approval_status in ('approved','stale')
                        and claim.approval_cycle==row.approval_id.number
                        and claim.replacement_conflict_cycle!=row.approval_id.number):
                    row.effective_value=row.claim_value
                    row.realized_value=row.realized_snapshot
            except (AccessError,ValidationError):
                continue

    def _audit(self,message,before):
        self.check_access('read')
        audit=self.env['supplier.shortage.claim.audit']
        super(ShortageClaimAudit,audit.sudo().with_context({})).create({
            'claim_id':self.claim_id.id,'company_id':self.company_id.id,'cash_settlement_id':self.id,
            'event_type':'cash_event','actor_id':self.env.uid,'event_datetime':fields.Datetime.now(),
            'change_summary':message,'reason':self.reason or self.note,'valuation_basis':{
                'old_state':before,'state':self.state,'kind':self.kind,'line':self.claim_line_id.id,
                'move':self.source_move_id.id,'move_line':self.source_line_id.id,'payment':self.payment_id.id,
                'bank':self.bank_id.id,'credit':self.credit_id.id,'partial':self.partial_id.id,
                'gross':self.gross_amount,'untaxed':self.untaxed_amount,'claim_value':self.claim_value,
                'realized':self.realized_snapshot,'currency':self.currency_id.id,
                'company_amount':self.company_amount,'company_currency':self.company_currency_id.id,
                'source_currency':(self.frozen_source_currency_id or self.source_currency_id).id,
                'fingerprint':self.source_fingerprint,'fx':self.fx_basis,'reversals':self.reversal_ids.ids,
            },
        })


class CashAudit(models.Model):
    _inherit='supplier.shortage.claim.audit'
    cash_settlement_id=fields.Many2one(MODEL,readonly=True,index=True,ondelete='restrict')
    event_type=fields.Selection(selection_add=[('cash_event','Cash and Payable Settlement')],
                                ondelete={'cash_event':lambda rows:None})

    def _access_domain(self,operation):
        return super()._access_domain(operation) & (
            Domain('cash_settlement_id','=',False) | Domain('cash_settlement_id','access','read'))


class CashClaimLine(models.Model):
    _inherit='supplier.shortage.claim.line'
    cash_settlement_ids=fields.One2many(MODEL,'claim_line_id',readonly=True,copy=False)
    cash_value=fields.Monetary(compute='_credit_position',aggregator=False)
    cash_realization=fields.Monetary(compute='_credit_position',aggregator=False)
    payable_realization=fields.Monetary(compute='_credit_position',aggregator=False)


class CashClaim(models.Model):
    _inherit='supplier.shortage.claim'
    cash_settlement_ids=fields.One2many(MODEL,'claim_id',readonly=True,copy=False)

    @api.model_create_multi
    def create(self,values):
        if any('cash_settlement_ids' in vals for vals in values) or 'default_cash_settlement_ids' in self.env.context:
            raise AccessError('Open the Cash Recovery workbench to manage settlements.')
        return super().create(values)

    def write(self,values):
        if 'cash_settlement_ids' in values:
            raise AccessError('Open the Cash Recovery workbench to manage settlements.')
        return super().write(values)

    @api.depends('replacement_ids.effective_value','supplier_position_currency_id','replacement_conflict_cycle',
                 'credit_allocation_ids.effective_value','cash_settlement_ids.effective_value')
    @api.depends_context('uid','allowed_company_ids')
    def _compute_verified_recovery(self):
        super()._compute_verified_recovery()
        model=self.env[MODEL]
        if model.has_access('read'):
            grouped=model.search([('claim_id','in',self.ids),('state','=','verified'),('kind','=','direct')]).grouped('claim_id')
            for claim in self:
                claim.verified_recovery_amount+=sum(grouped.get(claim,model.browse()).filtered(
                    lambda row:row.currency_id==claim.supplier_position_currency_id).mapped('effective_value'))

    def action_open_cash(self):
        self.ensure_one()
        self.check_access('read')
        self.env[MODEL].check_access('read')
        action=self.env['ir.actions.act_window']._for_xml_id(ROLE+'action_cash_settlement')
        action['domain']=[('claim_id','=',self.id)]
        if len(self.claim_line_ids)==1:
            action['context']={'default_claim_line_id':self.claim_line_ids.id}
        return action


class CashPartialReconcile(models.Model):
    _inherit='account.partial.reconcile'

    @api.model_create_multi
    def create(self,values):
        records=super().create(values)
        if self.env.registry.ready:
            moves=(records.credit_move_id | records.debit_move_id).move_id
            version_cash(self.env,moves.company_id.ids)
            self.env[MODEL].invalidate_model(['source_status','effective_value','realized_value'],flush=False)
        return records

    def unlink(self):
        moves=(self.credit_move_id | self.debit_move_id).move_id
        if self.env.registry.ready:
            cash_source_changed(self.env,moves-moves.filtered(lambda m:m.move_type=='in_refund'),
                                companies=moves.company_id.ids)
        result=super().unlink()
        if self.env.registry.ready:
            self.env[MODEL].invalidate_model(['partial_id','source_status','effective_value','realized_value'],flush=False)
        return result


class CashPayment(models.Model):
    _inherit='account.payment'

    def write(self,values):
        material=values.keys() & {'state','date','journal_id','payment_type','partner_type','partner_id','currency_id','move_id','amount'}
        prior=self.move_id if material else self.env['account.move']
        result=super().write(values)
        if material:
            cash_source_changed(self.env,prior | self.move_id)
        return result


class CashBankStatement(models.Model):
    _inherit='account.bank.statement.line'

    def write(self,values):
        result=super().write(values)
        if values.keys() & {'date','amount','amount_currency','foreign_currency_id','partner_id','journal_id','move_id'}:
            cash_source_changed(self.env,self.move_id)
        return result

class RecoveryLedger(models.Model):
    """A read-only view over the three authoritative allocation models."""
    _name='supplier.shortage.recovery.ledger'
    _description='Financial Recovery Ledger'
    _auto=False
    _rec_name='claim_id'
    _order='event_date desc, id desc'

    claim_id=fields.Many2one('supplier.shortage.claim',readonly=True)
    claim_line_id=fields.Many2one('supplier.shortage.claim.line',readonly=True)
    company_id=fields.Many2one('res.company',readonly=True)
    currency_id=fields.Many2one('res.currency',readonly=True)
    replacement_id=fields.Many2one('supplier.shortage.replacement',readonly=True)
    credit_id=fields.Many2one('supplier.shortage.credit.allocation',readonly=True)
    cash_id=fields.Many2one(MODEL,readonly=True)
    settlement_type=fields.Selection([('goods','Replacement goods'),('credit','Vendor credit'),
        ('direct','Direct cash'),('credit_cash','Credit cash realization'),('offset','Credit payable offset'),
        ('other','Unclassified cash proposal')],readonly=True)
    state=fields.Char(readonly=True)
    event_date=fields.Datetime(readonly=True)
    historical_value=fields.Monetary(readonly=True,aggregator=False)
    effective_value=fields.Monetary(compute='_amounts',aggregator=False)
    realization_value=fields.Monetary(compute='_amounts',aggregator=False)

    def init(self):
        from odoo.tools import drop_view_if_exists
        drop_view_if_exists(self.env.cr,self._table)
        self.env.cr.execute("""
            CREATE VIEW supplier_shortage_recovery_ledger AS
            SELECT r.id::bigint*4+1 AS id,r.claim_id,r.claim_line_id,r.company_id,r.currency_id,
                   r.id AS replacement_id,NULL::integer AS credit_id,NULL::integer AS cash_id,
                   'goods'::varchar AS settlement_type,r.state,coalesce(r.reversed_at,r.verified_at,r.create_date) AS event_date,
                   r.verified_goods_value AS historical_value
            FROM supplier_shortage_replacement r
            UNION ALL
            SELECT c.id::bigint*4+2,c.claim_id,c.claim_line_id,c.company_id,c.currency_id,NULL,c.id,NULL,
                   'credit',c.state,coalesce(c.reversed_at,c.verified_at,c.create_date),c.claim_value
            FROM supplier_shortage_credit_allocation c
            UNION ALL
            SELECT s.id::bigint*4+3,s.claim_id,s.claim_line_id,s.company_id,s.currency_id,NULL,NULL,s.id,
                   CASE s.kind WHEN 'credit' THEN 'credit_cash' WHEN 'offset' THEN 'offset'
                               WHEN 'direct' THEN 'direct' ELSE 'other' END,
                   s.state,coalesce(s.reversed_at,s.verified_at,s.create_date),s.claim_value
            FROM supplier_shortage_cash_settlement s
        """)

    def _access_domain(self,operation):
        source=Domain.FALSE
        for name in ('replacement_id','credit_id','cash_id'):
            source |= Domain(name,'!=',False) & Domain(name,'access','read')
        return super()._access_domain(operation) & source & Domain('claim_line_id','access','read')

    @api.depends_context('uid','allowed_company_ids')
    def _amounts(self):
        for row in self:
            allocation=row._allocation()
            allocation.check_access('read')
            row.effective_value=allocation.effective_value
            row.realization_value=allocation.realized_value if row.cash_id else 0

    def _allocation(self):
        return self.replacement_id or self.credit_id or self.cash_id

    def action_open_source(self):
        self.ensure_one()
        self.check_access('read')
        allocation=self._allocation()
        allocation.check_access('read')
        return {'type':'ir.actions.act_window','res_model':allocation._name,'res_id':allocation.id,
                'views':[(False,'form')]}
