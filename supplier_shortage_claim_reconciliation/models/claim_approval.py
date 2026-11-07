"""Company internal authorization, independent of supplier acceptance/recovery."""
import math
from datetime import timedelta
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from .claim_preparation import ROLE, lock_records
from .shortage_claim import ShortageClaim
from .evidence_verification import digest

ROLES = [('purchase', 'Purchase Manager'), ('accounts', 'Accounts Reviewer'), ('finance', 'Finance Manager')]
GROUPS = dict(purchase='group_claim_manager', accounts='group_accounts_reviewer', finance='group_finance_manager')
STATUS = [('none', 'Not Requested'), ('purchase', 'Awaiting Purchase'), ('accounts', 'Awaiting Accounts'),
          ('finance', 'Awaiting Finance'), ('approved', 'Internally Approved'), ('rejected', 'Rejected'),
          ('returned', 'Returned for Correction'), ('stale', 'Reapproval Required')]
OUTPUTS = {'approval_status', 'approval_cycle', 'approval_cycle_ids', 'approval_step_ids', 'verified_basis',
           'verified_amount', 'financial_reviewer_id', 'financial_reviewed_at', 'financial_review_note'}


class ApprovalPolicy(models.Model):
    _name = 'supplier.shortage.approval.policy'
    _description = 'Company Internal Claim Approval Policy'
    _check_company_auto = True
    _order = 'company_id, minimum_amount, id'
    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s:s.env.company, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    active = fields.Boolean(default=True)
    minimum_amount = fields.Monetary(required=True, default=0, help='Exclusive lower bound; zero includes zero.')
    maximum_amount = fields.Monetary(default=0, help='Inclusive upper bound. Zero means unbounded.')
    approval_chain = fields.Selection([('purchase','Purchase'), ('accounts','Purchase → Accounts'),
                                       ('finance','Purchase → Accounts → Finance')], required=True, default='purchase')
    separate_approvers_required = fields.Boolean(default=True)
    effective_from = fields.Date()
    effective_to = fields.Date()
    deadline_days = fields.Integer(default=7, help='Calendar days; informational, never automatic approval.')
    configuration_notes = fields.Text()

    def _require_config(self):
        if self.env.su or not self.env.user.has_group(ROLE+'group_module_admin'):
            raise AccessError('An authenticated module configuration administrator must maintain approval policies.')

    def _lock_configuration(self, companies):
        if not companies:
            return
        lock_records(companies)
        # A lock alone cannot prevent phantom ranges under Repeatable Read if a
        # second request already has an older snapshot. Version the company row
        # using metadata only, after role/company validation, so that request must
        # retry with fresh policy coverage. No company business data is changed.
        companies.flush_recordset(['write_date'])
        self.env.cr.execute('UPDATE res_company SET write_date=clock_timestamp() AT TIME ZONE \'UTC\' WHERE id IN %s',[tuple(companies.ids)])
        companies.invalidate_recordset(['write_date'])

    @api.model_create_multi
    def create(self, values):
        self._require_config()
        values=[dict(v,company_id=v.get('company_id') or self.env.company.id) for v in values]
        companies = self.env['res.company'].browse(sorted({v.get('company_id', self.env.company.id) for v in values}))
        if set(companies.ids)-set(self.env.companies.ids):
            raise AccessError('Configure policies only for activated authorized companies.')
        self._lock_configuration(companies)
        return super().create(values)

    def write(self, values):
        self._require_config()
        self.check_access('write')
        if 'company_id' in values:
            raise AccessError('Policy company is immutable; archive and create another policy.')
        self._lock_configuration(self.company_id)
        return super().write(values)

    def unlink(self):
        raise AccessError('Archive policies to preserve historical authorization references.')

    @api.constrains('minimum_amount','maximum_amount','active','effective_from','effective_to','deadline_days')
    def _check_ranges(self):
        for p in self:
            c = p.currency_id
            if not all(math.isfinite(x) for x in (p.minimum_amount,p.maximum_amount)) or min(p.minimum_amount,p.maximum_amount,p.deadline_days)<0:
                raise ValidationError('Approval limits and calendar deadline must be nonnegative and finite.')
            if p.maximum_amount and c.compare_amounts(p.maximum_amount,p.minimum_amount)<=0:
                raise ValidationError('Maximum must exceed the exclusive minimum.')
            if p.effective_from and p.effective_to and p.effective_from>p.effective_to:
                raise ValidationError('Invalid policy effective dates.')
            if not p.active:
                continue
            for q in self.search([('company_id','=',p.company_id.id),('active','=',True),('id','!=',p.id)]):
                dates = not ((p.effective_to and q.effective_from and p.effective_to<q.effective_from) or
                             (q.effective_to and p.effective_from and q.effective_to<p.effective_from))
                amounts = (not p.maximum_amount or c.compare_amounts(p.maximum_amount,q.minimum_amount)>0) and (not q.maximum_amount or c.compare_amounts(q.maximum_amount,p.minimum_amount)>0)
                if dates and amounts:
                    raise ValidationError('Active approval ranges overlap in amount and effective date.')

    def _snapshot(self):
        self.ensure_one()
        return {k:self[k] for k in ('name','minimum_amount','maximum_amount','approval_chain','separate_approvers_required','deadline_days')}

    @api.model
    def _select_for(self, company, amount, date):
        policies=self.search([('company_id','=',company.id),('active','=',True)])
        c=company.currency_id
        policy=policies.filtered(lambda p:(not p.effective_from or p.effective_from<=date) and (not p.effective_to or p.effective_to>=date)
            and (not p.minimum_amount or c.compare_amounts(amount,p.minimum_amount)>0)
            and (not p.maximum_amount or c.compare_amounts(amount,p.maximum_amount)<=0))
        if len(policy)!=1:
            raise ValidationError('Approval configuration gap or ambiguity: configure exactly one company policy covering the reviewed amount and submission date.')
        return policy


class ApprovalCycle(models.Model):
    _name = 'supplier.shortage.approval.cycle'
    _description = 'Frozen Internal Approval Cycle'
    _order = 'id desc'
    _check_company_auto = True
    claim_id = fields.Many2one('supplier.shortage.claim', required=True, ondelete='restrict', check_company=True, index=True)
    company_id = fields.Many2one('res.company', required=True, index=True)
    number = fields.Integer(required=True)
    policy_id = fields.Many2one('supplier.shortage.approval.policy', required=True, ondelete='restrict', check_company=True)
    snapshot = fields.Json(required=True)
    fingerprint = fields.Char(required=True)
    state = fields.Selection([('active','Active'),('approved','Approved'),('rejected','Rejected'),('returned','Returned'),('superseded','Superseded')], default='active', required=True)
    currency_id = fields.Many2one('res.currency', required=True)
    amount = fields.Monetary(required=True,aggregator=False)
    company_currency_id = fields.Many2one('res.currency', required=True)
    comparison_amount = fields.Monetary(currency_field='company_currency_id', required=True,aggregator=False)
    conversion_date = fields.Date(required=True)
    conversion_basis = fields.Json(required=True)
    step_ids = fields.One2many('supplier.shortage.claim.approval','cycle_id')
    _cycle_unique = models.Constraint('UNIQUE(claim_id,number)', 'Approval cycle already exists.')

    def _access_domain(self, operation):
        return super()._access_domain(operation) & ~Domain('claim_id.claim_line_ids','any!',~Domain('id','access','read'))

    @api.model_create_multi
    def create(self, values):
        raise AccessError('Approval cycles are created only by validated claim submission.')
    def write(self, values):
        raise AccessError('Frozen approval cycles cannot be edited directly.')
    def unlink(self):
        raise AccessError('Approval history cannot be deleted.')
    def copy(self, default=None):
        raise AccessError('Approval cycles cannot be copied.')


class ClaimApproval(models.Model):
    _name = 'supplier.shortage.claim.approval'
    _description = 'Internal Claim Approval Decision'
    _order = 'cycle_id desc, sequence, id'
    _check_company_auto = True
    cycle_id = fields.Many2one('supplier.shortage.approval.cycle',required=True,ondelete='restrict',check_company=True,index=True)
    claim_id = fields.Many2one(related='cycle_id.claim_id',store=True,index=True)
    company_id = fields.Many2one(related='cycle_id.company_id',store=True,index=True)
    partner_id = fields.Many2one(related='claim_id.partner_id')
    purchase_id = fields.Many2one(related='claim_id.purchase_id')
    claim_approval_status = fields.Selection(related='claim_id.approval_status',string='Current Authorization')
    sequence = fields.Integer(required=True)
    role = fields.Selection(ROLES,required=True)
    assigned_user_id = fields.Many2one('res.users',check_company=True)
    state = fields.Selection([('waiting','Waiting for Earlier Stage'),('pending','Pending'),('approved','Approved'),('rejected','Rejected'),('returned','Returned'),('superseded','Superseded')],required=True,index=True)
    decision_by = fields.Many2one('res.users',ondelete='restrict')
    decision_at = fields.Datetime()
    reason = fields.Text()
    amount = fields.Monetary(related='cycle_id.amount',aggregator=False)
    currency_id = fields.Many2one(related='cycle_id.currency_id')
    deadline = fields.Date()
    days_pending = fields.Integer(compute='_compute_age')
    overdue = fields.Boolean(compute='_compute_age')
    can_decide = fields.Boolean(compute='_compute_can_decide')
    _stage_unique = models.Constraint('UNIQUE(cycle_id,sequence)', 'This cycle already contains this stage.')

    def _compute_age(self):
        today=fields.Date.today()
        for r in self:
            r.days_pending=max((today-r.create_date.date()).days,0) if r.create_date and r.state=='pending' else 0
            r.overdue=bool(r.state=='pending' and r.deadline and r.deadline<today)

    @api.depends('state','assigned_user_id','claim_id.approval_status','cycle_id.state')
    @api.depends_context('uid','allowed_company_ids')
    def _compute_can_decide(self):
        for r in self:
            try:
                r.claim_id._check_approver(r.role,self.env.user,r.cycle_id)
                r.can_decide=(r.state=='pending' and r.claim_id.approval_status==r.role and
                    (not r.assigned_user_id or r.assigned_user_id==self.env.user))
            except AccessError:
                r.can_decide=False

    def action_open_decision(self):
        self.ensure_one()
        self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':'supplier.shortage.approval.wizard','view_mode':'form',
                'target':'new','context':{'default_step_id':self.id,'default_operation':'reject'}}

    @api.model
    def action_my_pending(self):
        roles=[role for role,group in GROUPS.items() if self.env.user.has_group(ROLE+group)]
        return {'type':'ir.actions.act_window','name':'My Pending Approvals','res_model':self._name,'view_mode':'list,form',
            'domain':[('state','=','pending'),('role','in',roles),('claim_id.approval_status','in',roles),
                ('claim_id.create_uid','!=',self.env.uid),('claim_id.user_id','!=',self.env.uid),
                ('claim_id.review_requested_by','!=',self.env.uid),'|',('assigned_user_id','=',False),('assigned_user_id','=',self.env.uid)]}

    @api.model_create_multi
    def create(self, values):
        raise AccessError('Approval requests are server controlled.')
    def write(self, values):
        raise AccessError('Use controlled approval decisions and reassignment.')
    def unlink(self):
        raise AccessError('Approval decisions cannot be deleted.')
    def copy(self, default=None):
        raise AccessError('Approval decisions cannot be copied.')

    def _decide(self, decision, reason=None):
        self.ensure_one()
        self.check_access('read')
        with self.env.cr.savepoint():
            claim=self.claim_id
            claim._approval_lock()
            self.invalidate_recordset()
            cycle=self.cycle_id
            cycle.invalidate_recordset()
            if cycle.number!=claim.approval_cycle or cycle.state!='active' or self.state!='pending' or claim.approval_status!=self.role:
                raise ValidationError('This is not the current pending approval stage.')
            claim._check_approver(self.role,self.env.user,cycle)
            if self.assigned_user_id and self.assigned_user_id!=self.env.user:
                raise AccessError('Only the assigned authorized approver may decide this stage.')
            if cycle.step_ids.filtered(lambda s:s.sequence<self.sequence and s.state!='approved'):
                raise ValidationError('Earlier approval stages must pass first.')
            if decision!='approved' and len((reason or '').strip())<5:
                raise ValidationError('A meaningful rejection or correction reason is required.')
            # Reject/return do not attest financial or evidential validity.
            if decision=='approved':
                claim._approval_ready()
                if digest(claim._approval_basis())!=cycle.fingerprint:
                    raise ValidationError('The approval basis has changed; obtain correction and a new approval cycle.')
            super(ClaimApproval,self).write({'state':decision,'decision_by':self.env.uid,'decision_at':fields.Datetime.now(),'reason':reason})
            if decision=='approved':
                following=cycle.step_ids.filtered(lambda s:s.sequence>self.sequence).sorted('sequence')[:1]
                if following:
                    super(ClaimApproval,following).write({'state':'pending'})
                    status=following.role
                else:
                    status='approved'
                    super(ApprovalCycle,cycle).write({'state':'approved'})
            else:
                status=decision
                super(ClaimApproval,cycle.step_ids.filtered(lambda s:s.state in ('pending','waiting'))).write({'state':'superseded'})
                super(ApprovalCycle,cycle).write({'state':decision})
            claim._approval_set({'approval_status':status})
            claim._approval_audit('Stage %s: %s%s'%(self.role,decision,'; final internal approval completed' if status=='approved' else ''),reason,cycle)
        return True

    def action_approve(self):
        return self._decide('approved')
    def action_reject(self, reason=None):
        return self._decide('rejected',reason)
    def action_return_for_correction(self, reason=None):
        return self._decide('returned',reason)

    def action_reassign(self, user_id, reason):
        self.ensure_one()
        self.check_access('read')
        with self.env.cr.savepoint():
            self.claim_id._approval_lock()
            self.invalidate_recordset()
            if not self.env.user.has_group(ROLE+'group_module_admin') or self.state!='pending' or self.cycle_id.state!='active' or self.claim_id.approval_status!=self.role:
                raise AccessError('Only a configuration administrator may reassign a current pending stage.')
            if len((reason or '').strip())<5:
                raise ValidationError('Provide a meaningful reassignment reason.')
            user=self.env['res.users'].browse(user_id).exists()
            user.ensure_one()
            self.claim_id._check_approver(self.role,user,self.cycle_id)
            old=self.assigned_user_id
            super(ClaimApproval,self).write({'assigned_user_id':user.id})
            self.claim_id._approval_audit('Approver reassigned: %s → %s'%(old.id or 'pool',user.id),reason,self.cycle_id)
        return True


class InternalClaimApproval(models.Model):
    _inherit = 'supplier.shortage.claim'
    approval_status = fields.Selection(STATUS,readonly=True,default='none',copy=False,index=True,tracking=True)
    approval_cycle = fields.Integer(readonly=True,copy=False)
    approval_cycle_ids = fields.One2many('supplier.shortage.approval.cycle','claim_id',readonly=True,copy=False)
    approval_step_ids = fields.One2many('supplier.shortage.claim.approval','claim_id',readonly=True,copy=False)
    verified_basis = fields.Json(readonly=True,copy=False)
    verified_amount = fields.Monetary(readonly=True,copy=False,aggregator=False)
    financial_reviewer_id = fields.Many2one('res.users',readonly=True,copy=False)
    financial_reviewed_at = fields.Datetime(readonly=True,copy=False)
    financial_review_note = fields.Text(readonly=True,copy=False)

    @api.model_create_multi
    def create(self, values):
        if any(OUTPUTS & v.keys() for v in values) or any('default_'+k in self.env.context for k in OUTPUTS):
            raise AccessError('Approval and verified financial fields are server controlled.')
        return super().create(values)

    def write(self, values):
        if OUTPUTS & values.keys():
            raise AccessError('Approval and verified financial fields are server controlled.')
        if values.keys() & {'partner_id','purchase_id','company_id','user_id','claim_date','claim_line_ids','note',
                            'shortage_reason','evidence_scenario','manual_exception_reason','correction_reason','evidence_ids'}:
            if not self.env.su and not self.env.user.has_group(ROLE+'group_claim_user'):
                raise AccessError('Financial workflow access does not grant claim preparation authority.')
        return super().write(values)

    def _approval_set(self, values):
        # Private, context-independent mutation below existing claim guards.
        # Audit records are explicit. Never notify external mail.thread followers
        # (including a supplier manually subscribed to the claim) about approval.
        return super(ShortageClaim,self.with_context(tracking_disable=True)).write(values)

    def _approval_lock(self):
        self.ensure_one()
        self.check_access('write')
        if self.env.su or self.company_id not in self.env.companies or self.company_id not in self.env.user.company_ids:
            raise AccessError('Approval requires an authenticated business user in the activated claim company.')
        self._lock_for_edit()
        self.invalidate_recordset()

    def _approval_audit(self, summary, reason=None, cycle=None):
        self._audit('approval_event',summary,reason=reason,basis={self.id:{'cycle':cycle.number if cycle else self.approval_cycle,
            'cycle_id':cycle.id if cycle else False,'approval_status':self.approval_status,
            'amount':cycle.amount if cycle else self.verified_amount,'currency_id':self.currency_id.id}})

    def _approval_ready(self):
        if self.state!='under_review':
            raise ValidationError('Submit the claim for evidence review first.')
        self._validate_for_review()
        self.claim_line_ids._check_submitted_purchase_source()
        if any(e.effective_state!='verified' for e in self.evidence_ids.filtered('is_current')):
            raise ValidationError('All current supporting evidence must be verified.')

    def _financial_basis(self):
        result=[]
        for line in self._review_lines():
            po=line.purchase_line_id
            bills=po.invoice_lines.filtered(lambda b:b.move_id.state=='posted' and b.move_id.move_type in ('in_invoice','in_refund') and b.display_type=='product')
            bills.check_access('read')
            lock_records(bills.move_id)
            lock_records(bills)
            if len(bills)!=1 or bills.move_id.move_type!='in_invoice':
                raise ValidationError('Accounts review required: valuation needs one unambiguous posted invoice line per purchase line; split billing/credits require reconciliation.')
            bill=bills
            bill.move_id.check_access('read')
            if bill.currency_id!=self.currency_id or bill.company_id!=self.company_id or bill.partner_id.commercial_partner_id!=self.partner_id.commercial_partner_id or bill.quantity<=0 or bill.price_subtotal<0:
                raise ValidationError('Posted bill company, supplier, quantity or currency is ambiguous.')
            if not bill.product_uom_id._has_common_reference(po.uom_id):
                raise ValidationError('Bill and purchase UoM must have a compatible reference.')
            qty=po.uom_id._compute_quantity(line.allocated_qty,bill.product_uom_id,round=False)
            if bill.product_uom_id.compare(qty,bill.quantity)>0:
                raise ValidationError('Reviewed claim quantity exceeds posted bill quantity.')
            amount=self.currency_id.round(bill.price_subtotal*qty/bill.quantity)
            if not math.isfinite(amount) or amount<0:
                raise ValidationError('Invalid reviewed untaxed valuation.')
            result.append({'line_id':line.id,'purchase_line_id':po.id,'bill_line_id':bill.id,'bill_id':bill.move_id.id,
                'bill_name':bill.move_id.name,'bill_date':str(bill.move_id.invoice_date),'bill_write_date':str(bill.write_date),
                'quantity':line.allocated_qty,'uom_id':po.uom_id.id,'uom_factor':po.uom_id.factor,
                'bill_quantity':qty,'bill_uom_id':bill.product_uom_id.id,'bill_uom_factor':bill.product_uom_id.factor,
                'invoice_quantity':bill.quantity,'price_unit':bill.price_unit,'discount':bill.discount,
                'invoice_untaxed_subtotal':bill.price_subtotal,'tax_ids':bill.tax_ids.ids,
                'tax_basis':'native posted bill untaxed subtotal prorated by verified quantity',
                'amount':amount,'currency_id':bill.currency_id.id,'source_signature':line.detection_id.source_signature})
        return result

    def action_verify_financial_value(self, note=None):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._approval_lock()
            if not self.env.user.has_group(ROLE+'group_accounts_reviewer') or self.env.user in (self.create_uid,self.user_id,self.review_requested_by):
                raise AccessError('An independent Accounts Reviewer must verify the financial basis.')
            if self.approval_status not in ('none','returned','rejected','stale'):
                raise ValidationError('Start a correction cycle before replacing a submitted financial basis.')
            if len((note or '').strip())<5:
                raise ValidationError('Record the financial verification justification.')
            self._approval_ready()
            basis=self._financial_basis()
            self._approval_set({'verified_basis':basis,'verified_amount':sum(r['amount'] for r in basis),
                'financial_reviewer_id':self.env.uid,'financial_reviewed_at':fields.Datetime.now(),'financial_review_note':note})
            self._approval_audit('Untaxed posted-bill valuation independently verified.',note)
        return True

    def _approval_basis(self):
        basis=self._financial_basis()
        if not self.financial_reviewer_id or not self.verified_basis or basis!=self.verified_basis:
            raise ValidationError('Current financial basis requires explicit Accounts verification; provisional estimates cannot authorize approval.')
        return {'claim':self.name,'company':self.company_id.id,'supplier':self.partner_id.id,'purchase':self.purchase_id.id,
            'currency':self.currency_id.id,'financial_lines':basis,'reviewer':self.financial_reviewer_id.id,
            'reviewed_at':str(self.financial_reviewed_at),'readiness':self.readiness_fingerprint,
            'evidence':[{'id':e.id,'state':e.effective_state,'uploader':e.create_uid.id,'verifier':e.verified_by.id,
                'verified_at':str(e.verified_at),'fingerprint':e.reviewed_fingerprint} for e in self.evidence_ids.filtered('is_current').sorted('id')],
            'warehouse':[{'id':o.id,'recorder':o.recorded_by.id,'verifier':o.verified_by.id,'verified_at':str(o.verified_at),
                'fingerprint':o.reviewed_fingerprint} for o in self.warehouse_observation_ids.filtered('is_current').sorted('id')],
            'maker':self.create_uid.id,'preparer':self.user_id.id,'submitted_by':self.review_requested_by.id}

    def _conversion(self):
        currency=self.currency_id; target=self.company_id.currency_id; date=fields.Date.today()
        rate=1.0; rows=[]
        if currency!=target:
            # Match this installed Odoo version's actual selection/fallback rules,
            # rather than independently guessing a date/order for the rate row.
            native_rates=(currency|target)._get_rates(self.company_id,date)
            for cur in currency|target:
                native_rate,rate_date=native_rates[cur.id]
                if not rate_date or rate_date>date or not math.isfinite(native_rate) or native_rate<=0:
                    raise ValidationError('Odoo has no documented valid exchange rate on or before submission date; financial review is required.')
                r=self.env['res.currency.rate'].search([('currency_id','=',cur.id),('name','=',rate_date),
                    ('company_id','in',[False,self.company_id.root_id.id])],order='company_id.id, name desc',limit=1)
                if not r or not math.isfinite(r.rate) or r.rate<=0 or not math.isclose(r.rate,native_rate,rel_tol=1e-12):
                    raise ValidationError('A documented valid exchange rate is required for both currencies on or before submission date.')
                rows.append({'id':r.id,'currency_id':cur.id,'date':str(r.name),'rate':r.rate,'company_id':r.company_id.id})
            rate=currency._get_conversion_rate(currency,target,self.company_id,date)
            if not math.isfinite(rate) or rate<=0:
                raise ValidationError('Invalid approval conversion rate.')
        return date, target.round(self.verified_amount*rate), {'date':str(date),'rate':rate,'source_rates':rows,'currency_id':currency.id,'company_currency_id':target.id}

    def action_request_internal_approval(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._approval_lock()
            if not self.env.user.has_group(ROLE+'group_claim_user'):
                raise AccessError('Only claim preparers may request internal approval.')
            if self.approval_status not in ('none','returned','rejected','stale'):
                raise ValidationError('An effective approval request already exists; use a correction cycle.')
            self._approval_ready()
            basis=self._approval_basis()
            date,amount,conversion=self._conversion()
            policy=self.env['supplier.shortage.approval.policy']._select_for(self.company_id,amount,date)
            lock_records(policy)
            number=self.approval_cycle+1
            old=self.approval_cycle_ids.filtered(lambda r:r.number==self.approval_cycle)
            if old:
                super(ApprovalCycle,old).write({'state':'superseded'})
                super(ClaimApproval,old.step_ids.filtered(lambda r:r.state in ('waiting','pending'))).write({'state':'superseded'})
            snapshot={'basis':basis,'policy':policy._snapshot(),'conversion':conversion,'requested_by':self.env.uid,'requested_at':str(fields.Datetime.now()),'cycle':number}
            snapshot['source_labels']={'company':self.company_id.display_name,'supplier':self.partner_id.display_name,
                'purchase':self.purchase_id.name,'products':{str(l.id):l.product_id.display_name for l in self._review_lines()}}
            snapshot['commercial_context']={'claim_date':str(self.claim_date),'reason':self.shortage_reason,'note':self.note,
                'lines':[{'line_id':l.id,'claimed_quantity':l.proposed_qty,'claim_uom_id':l.claim_uom_id.id,
                          'claim_uom_name':l.claim_uom_id.display_name,'claim_uom_factor':l.claim_uom_id.factor}
                         for l in self._review_lines()]}
            # Narrow insertion-only elevation after access, role, company and readiness validation.
            cycle=super(ApprovalCycle,self.env['supplier.shortage.approval.cycle'].sudo().with_context({})).create({
                'claim_id':self.id,'company_id':self.company_id.id,'number':number,'policy_id':policy.id,'snapshot':snapshot,
                'fingerprint':digest(basis),'currency_id':self.currency_id.id,'amount':self.verified_amount,
                'company_currency_id':self.company_id.currency_id.id,'comparison_amount':amount,'conversion_date':date,'conversion_basis':conversion})
            roles=['purchase','accounts','finance'][:{'purchase':1,'accounts':2,'finance':3}[policy.approval_chain]]
            super(ClaimApproval,self.env['supplier.shortage.claim.approval'].sudo().with_context({})).create([
                {'cycle_id':cycle.id,'sequence':i+1,'role':role,'state':'pending' if i==0 else 'waiting',
                 'deadline':date+timedelta(days=policy.deadline_days) if policy.deadline_days else False} for i,role in enumerate(roles)])
            self._approval_set({'approval_cycle':number,'approval_status':roles[0]})
            if old:
                self._approval_audit('Previous approval cycle %s superseded by cycle %s; historical decisions retained.'%(old.number,number),cycle=old)
            self._approval_audit('Approval requested; frozen policy %s; required stages: %s.'%(policy.name,', '.join(roles)),cycle=cycle)
        return True

    def _check_approver(self, role, user, cycle):
        if not user.active or user.share or not user.has_group(ROLE+GROUPS[role]) or self.company_id not in user.company_ids:
            raise AccessError('Approver lacks the required business role or company authorization.')
        basis=cycle.snapshot['basis']
        makers={basis['maker'],basis['preparer'],basis['submitted_by'],self.create_uid.id,self.user_id.id,self.review_requested_by.id}
        if user.id in makers:
            raise AccessError('Claim makers, preparers and submitters cannot approve their own claim.')
        if cycle.snapshot['policy']['separate_approvers_required'] and user in cycle.step_ids.filtered(lambda r:r.state=='approved').decision_by:
            raise AccessError('This policy requires an independent person at each approval stage.')

    def action_start_approval_correction(self, reason=None):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._approval_lock()
            if not self.env.user.has_group(ROLE+'group_claim_manager') or self.approval_status not in ('returned','rejected','stale','approved') or len((reason or '').strip())<5:
                raise AccessError('A claim manager must document correction of a completed, returned, rejected or stale approval.')
            status='stale' if self.approval_status=='approved' else self.approval_status
            self._approval_set({'state':'draft','approval_status':status,'verified_basis':False,'verified_amount':0,'financial_reviewer_id':False,
                'financial_reviewed_at':False,'financial_review_note':False})
            self._approval_audit('Approval correction opened; allocations remain reserved.',reason)
        return True

    def action_return_to_draft(self):
        if any(c.approval_status!='none' for c in self):
            raise ValidationError('Use the approval correction workflow; approval history must be retained.')
        return super().action_return_to_draft()

    def _invalidate_evidence_readiness(self, reason):
        result=super()._invalidate_evidence_readiness(reason)
        if self and (self.env.registry.ready or getattr(self.env.registry,'_ssc_approval_schema_ready',False)):
            from .source_evidence_invalidation import invalidate_approvals
            lines=self.claim_line_ids
            if lines:
                invalidate_approvals(self.env,lines.ids,reason)
        return result

    def _auto_init(self):
        result=super()._auto_init()
        self.env.registry._ssc_approval_schema_ready=True
        return result

    def action_open_approval_review(self):
        self.ensure_one()
        self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':'supplier.shortage.approval.wizard','view_mode':'form',
                'target':'new','context':{'default_claim_id':self.id,'default_operation':'verify'}}


class ApprovalWizard(models.TransientModel):
    _name='supplier.shortage.approval.wizard'
    _description='Document Internal Approval Review'
    claim_id=fields.Many2one('supplier.shortage.claim')
    step_id=fields.Many2one('supplier.shortage.claim.approval')
    operation=fields.Selection([('verify','Verify Financial Value'),('correction','Open Draft Correction'),
        ('reject','Reject'),('return','Return for Correction'),('reassign','Reassign')],required=True)
    reason=fields.Text(required=True)
    assigned_user_id=fields.Many2one('res.users')

    def action_apply(self):
        self.ensure_one()
        self.check_access('write')
        if self.operation in ('verify','correction'):
            self.claim_id.ensure_one()
            if self.operation=='verify':
                self.claim_id.action_verify_financial_value(self.reason)
            else:
                self.claim_id.action_start_approval_correction(self.reason)
        else:
            self.step_id.ensure_one()
            if self.operation=='reject': self.step_id.action_reject(self.reason)
            elif self.operation=='return': self.step_id.action_return_for_correction(self.reason)
            else:
                self.assigned_user_id.ensure_one()
                self.step_id.action_reassign(self.assigned_user_id.id,self.reason)
        return {'type':'ir.actions.act_window_close'}


class ApprovalAudit(models.Model):
    _inherit = 'supplier.shortage.claim.audit'
    event_type = fields.Selection(selection_add=[('approval_event','Internal Approval Event')],ondelete={'approval_event':lambda records: None})


class ApprovalLineGuard(models.Model):
    _inherit='supplier.shortage.claim.line'

    def write(self, values):
        if not self.env.su and not self.env.user.has_group(ROLE+'group_claim_user'):
            raise AccessError('Financial review access does not grant claim-line editing authority.')
        return super().write(values)
