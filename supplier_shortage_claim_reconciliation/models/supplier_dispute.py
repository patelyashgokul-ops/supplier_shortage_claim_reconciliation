"""Immutable negotiation/commitment events and controlled dispute cases."""
import math
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from .claim_preparation import ROLE
from .claim_submission import SUBMISSION
from .supplier_response import RESPONSE, authority, current_submission, version_submission


class SupplierCaseBase(models.AbstractModel):
    _name = 'supplier.shortage.response.scope'
    _description = 'Submitted Supplier Case Scope'
    _check_company_auto = True
    _allow_sudo_commands = False
    submission_id = fields.Many2one(SUBMISSION, required=True, check_company=True, ondelete='restrict', index=True)
    claim_id = fields.Many2one(related='submission_id.claim_id', store=True, index=True)
    company_id = fields.Many2one(related='submission_id.company_id', store=True, index=True)
    supplier_id = fields.Many2one(related='submission_id.supplier_id')
    revision = fields.Integer(related='submission_id.revision')
    response_id = fields.Many2one(RESPONSE, check_company=True, ondelete='restrict')
    description = fields.Text(required=True)
    recorded_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    event_date = fields.Datetime(readonly=True)

    def _access_domain(self, operation):
        return super()._access_domain(operation) & Domain('submission_id','access','read') & ~Domain('evidence_ids','any!',~Domain('id','access','read'))

    def _validate_scope(self):
        for rec in self:
            authority(rec)
            if rec.submission_id.state != 'sent' or len((rec.description or '').strip())<20:
                raise ValidationError('Use a sent submission and a substantive description.')
            if rec.response_id:
                rec.response_id.check_access('read')
                if rec.response_id.submission_id != rec.submission_id:
                    raise ValidationError('Response and case must reference the same submitted revision.')
            for evidence in rec.evidence_ids:
                evidence.check_access('read')
                if evidence.claim_id != rec.claim_id or evidence.company_id != rec.company_id:
                    raise ValidationError('Evidence must belong to the same claim and company.')
                if evidence.attachment_id:
                    evidence.attachment_id.check_access('read')

    def unlink(self):
        raise AccessError('Supplier case and negotiation history must be retained.')

    def copy(self, default=None):
        raise AccessError('Record a distinct documented event instead of copying history.')


class Negotiation(models.Model):
    _name = 'supplier.shortage.claim.negotiation'
    _inherit = 'supplier.shortage.response.scope'
    _description = 'Supplier Negotiation Event'
    _order = 'id desc'
    _rec_name = 'description'
    evidence_ids = fields.Many2many('supplier.shortage.claim.evidence', 'ssc_neg_evidence_rel', 'negotiation_id', 'evidence_id', check_company=True)
    negotiation_type = fields.Selection([('supplier_offer','Supplier Offer'),('counteroffer','Company Counteroffer'),
        ('revised','Supplier Revised Offer'),('company_response','Company Response'),('final','Supplier Final Position')], required=True)
    proposed_amount = fields.Monetary(required=True)
    currency_id = fields.Many2one('res.currency', required=True, ondelete='restrict')
    tax_basis = fields.Selection([('untaxed','Untaxed'),('inclusive','Tax Inclusive'),('unknown','Unspecified')], required=True, default='unknown')
    financial_review_required = fields.Boolean(compute='_review_required')
    claim_line_ids = fields.Many2many('supplier.shortage.claim.line', 'ssc_neg_line_rel', 'negotiation_id', 'line_id', check_company=True)
    supersedes_event_id = fields.Many2one('supplier.shortage.claim.negotiation', check_company=True, ondelete='restrict')
    status = fields.Selection([('proposed','Proposed'),('accounts','Accounts Reviewed'),('authorized','Internally Authorized Proposal'),('declined','Declined')], readonly=True, default='proposed', required=True)
    accounts_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    decided_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    decided_at = fields.Datetime(readonly=True)
    decision_note = fields.Text()

    @api.depends('currency_id','tax_basis','submission_id.currency_id','proposed_amount','submission_id.amount')
    def _review_required(self):
        for rec in self:
            rec.financial_review_required = rec.currency_id != rec.submission_id.currency_id or rec.tax_basis != 'untaxed' or rec.proposed_amount != rec.submission_id.amount

    @api.model_create_multi
    def create(self, values):
        allowed = {'submission_id','response_id','evidence_ids','description','negotiation_type','proposed_amount','currency_id','tax_basis','claim_line_ids','supersedes_event_id'}
        if any(set(v)-allowed for v in values):
            raise AccessError('Negotiation decisions and actor fields are server controlled.')
        records = super(Negotiation,self.with_context({})).create([dict(v,recorded_by=self.env.uid,event_date=fields.Datetime.now()) for v in values])
        for rec in records:
            rec._validate_scope(); rec.submission_id._lock()
            if not math.isfinite(rec.proposed_amount) or rec.proposed_amount<0:
                raise ValidationError('Use a finite nonnegative offer in its original currency.')
            valid = {r['line_id'] for r in rec.submission_id.external_snapshot['lines']}
            rec.claim_line_ids.check_access('read')
            if set(rec.claim_line_ids.ids)-valid:
                raise ValidationError('Negotiation scope contains a line outside the submitted revision.')
            if rec.supersedes_event_id:
                rec.supersedes_event_id.check_access('read')
                if rec.supersedes_event_id.submission_id != rec.submission_id or rec.supersedes_event_id.negotiation_type == 'counteroffer':
                    raise ValidationError('A revised supplier offer must reference an earlier supplier position in this revision.')
            rec.submission_id._audit('Negotiation event %s: %s %s %s. Proposal only; no acceptance or recovery.' % (rec.id,rec.negotiation_type,rec.proposed_amount,rec.currency_id.name),rec.description)
            version_submission(rec.submission_id)
        return records

    def write(self, values):
        if set(values) != {'decision_note'}:
            raise AccessError('Negotiation terms are immutable; record a revised event.')
        for rec in self:
            rec.check_access('write'); rec._validate_scope_read(); rec.submission_id._lock()
            if self.env.su or not any(self.env.user.has_group(ROLE+g) for g in ('group_claim_manager','group_accounts_reviewer','group_finance_manager')):
                raise AccessError('A commercial or financial reviewer must record the decision note.')
            if rec.status not in ('proposed','accounts'):
                raise AccessError('Final decision notes are immutable.')
        return super().write(values)

    def action_accounts_review(self):
        self.ensure_one(); self.check_access('write')
        if self.env.su or not self.env.user.has_group(ROLE+'group_accounts_reviewer') or self.recorded_by == self.env.user:
            raise AccessError('An independent Accounts Reviewer must assess commercial terms.')
        self.submission_id._lock(); self.invalidate_recordset()
        if self.status != 'proposed' or len((self.decision_note or '').strip())<20:
            raise ValidationError('Review a proposed event with a substantive decision note.')
        self._validate_scope_read()
        super(Negotiation,self).write({'status':'accounts','accounts_by':self.env.uid})
        self.submission_id._audit('Negotiation %s reviewed by Accounts; no agreement or recovery.' % self.id,self.decision_note)

    def _validate_scope_read(self):
        self.submission_id.check_access('read')
        self.claim_id.check_access('read')
        if self.company_id not in self.env.companies or self.company_id not in self.env.user.company_ids:
            raise AccessError('Activate the authorized case company.')

    def action_authorize(self):
        self.ensure_one(); authority(self,True); self.submission_id._lock(); self.invalidate_recordset()
        if not self.env.user.has_group(ROLE+'group_finance_manager') or self.env.user in (self.recorded_by,self.accounts_by):
            raise AccessError('Independent Finance authorization is required for commercial proposals.')
        if self.status != 'accounts' or len((self.decision_note or '').strip())<20:
            raise ValidationError('Accounts review and a substantive authorization note are required.')
        # Cross-currency/tax comparison and concessions remain review-only until later settlement steps.
        if self.currency_id != self.submission_id.currency_id or self.tax_basis != 'untaxed':
            raise ValidationError('Currency/tax basis is incompatible. Record a normalized reviewed proposal; do not infer exchange rates or tax recovery.')
        current_submission(self.submission_id)
        super(Negotiation,self).write({'status':'authorized','decided_by':self.env.uid,'decided_at':fields.Datetime.now()})
        self.submission_id._audit('Negotiation %s internally authorized as a proposal only; submitted claim and recovery unchanged.' % self.id,self.decision_note)

    def action_decline(self):
        self.ensure_one(); authority(self,True); self.submission_id._lock(); self.invalidate_recordset()
        if self.status not in ('proposed','accounts') or len((self.decision_note or '').strip())<20:
            raise ValidationError('Decline a pending proposal with a documented reason.')
        super(Negotiation,self).write({'status':'declined','decided_by':self.env.uid,'decided_at':fields.Datetime.now()})
        self.submission_id._audit('Negotiation %s declined internally.' % self.id,self.decision_note)


class Commitment(models.Model):
    _name = 'supplier.shortage.claim.commitment'
    _inherit = 'supplier.shortage.response.scope'
    _description = 'Supplier Promise (Not a Settlement)'
    _rec_name = 'description'
    _order = 'id desc'
    evidence_ids = fields.Many2many('supplier.shortage.claim.evidence', 'ssc_commit_evidence_rel', 'commitment_id', 'evidence_id', check_company=True)
    commitment_type = fields.Selection([('credit','Issue Vendor Credit'),('replacement','Send Replacement'),('refund','Process Refund'),
        ('evidence','Provide Evidence'),('delivery','Confirm Delivery'),('response','Respond'),('reconsider','Reconsider Rejection')], required=True)
    committed_amount = fields.Monetary()
    currency_id = fields.Many2one('res.currency', required=True)
    committed_qty = fields.Float(digits='Product Unit')
    claim_line_id = fields.Many2one('supplier.shortage.claim.line', check_company=True, ondelete='restrict')
    product_uom_id = fields.Many2one('uom.uom')
    promised_date = fields.Date(required=True)
    responsible_id = fields.Many2one('res.users', required=True, default=lambda s:s.env.user, check_company=True)
    supersedes_id = fields.Many2one('supplier.shortage.claim.commitment', check_company=True, ondelete='restrict')
    status = fields.Selection([('promised','Promised'),('pending','Fulfilled - Pending Verification'),('canceled','Canceled'),('revised','Revised')], default='promised', readonly=True, required=True)
    overdue = fields.Boolean(compute='_overdue', search='_search_overdue')
    status_note = fields.Text()

    @api.depends('promised_date','status')
    def _overdue(self):
        for rec in self:
            rec.overdue = rec.status == 'promised' and rec.promised_date < fields.Date.today()

    def _search_overdue(self, operator, value):
        domain = Domain('status','=','promised') & Domain('promised_date','<',fields.Date.today())
        return domain if (operator in ('=','in') and value in (True,[True])) else ~domain

    @api.model_create_multi
    def create(self, values):
        allowed = {'submission_id','response_id','evidence_ids','description','commitment_type','committed_amount','currency_id','committed_qty',
                   'claim_line_id','product_uom_id','promised_date','responsible_id','supersedes_id'}
        if any(set(v)-allowed for v in values):
            raise AccessError('Commitment status and provenance are server controlled.')
        records = super(Commitment,self.with_context({})).create([dict(v,recorded_by=self.env.uid,event_date=fields.Datetime.now()) for v in values])
        for rec in records:
            rec._validate_scope(); rec.submission_id._lock()
            if not all(math.isfinite(v) and v>=0 for v in (rec.committed_amount,rec.committed_qty)):
                raise ValidationError('Use finite nonnegative commitment values.')
            if not rec.response_id or rec.response_id.verification_state != 'verified':
                raise ValidationError('A promise needs a verified supporting supplier response.')
            if rec.company_id not in rec.responsible_id.company_ids or rec.responsible_id.share:
                raise ValidationError('Assign an internal employee authorized for this company.')
            if rec.committed_qty:
                rows = [r for r in rec.submission_id.external_snapshot['lines'] if r['line_id']==rec.claim_line_id.id]
                if len(rows)!=1 or rec.product_uom_id.id!=rows[0]['uom_id'] or rec.committed_qty>rows[0]['quantity']:
                    raise ValidationError('Quantity promises require an exact submitted line, its UoM and bounded quantity.')
            if rec.currency_id == rec.submission_id.currency_id and rec.committed_amount>rec.submission_id.amount:
                raise ValidationError('A promise cannot exceed the submitted value on the same currency basis.')
            if rec.supersedes_id:
                old=rec.supersedes_id
                old.check_access('read')
                if old.submission_id!=rec.submission_id or old.status!='promised':
                    raise ValidationError('Revise only a current promise on this revision.')
                super(Commitment,old).write({'status':'revised'})
            rec.submission_id._audit('Supplier commitment %s recorded. Promise only; not verified settlement.' % rec.id,rec.description)
            version_submission(rec.submission_id)
        return records

    def write(self, values):
        if set(values) != {'status_note'}:
            raise AccessError('Commitment promises are immutable; record a revised promise.')
        for rec in self:
            authority(rec,True); rec.submission_id._lock()
            if rec.status not in ('promised','pending'):
                raise AccessError('Historical commitment notes are immutable.')
        return super().write(values)

    def _change_status(self, status):
        self.ensure_one(); authority(self,True); self.submission_id._lock(); self.invalidate_recordset()
        if self.status not in ('promised','pending') or len((self.status_note or '').strip())<20:
            raise ValidationError('A current promise and substantive status explanation are required.')
        if self.status==status:
            return
        super(Commitment,self).write({'status':status})
        self.submission_id._audit('Commitment %s changed to %s; no financial recovery recorded.' % (self.id,status),self.status_note)

    def action_pending_verification(self):
        self._change_status('pending')

    def action_cancel(self):
        self._change_status('canceled')


class Dispute(models.Model):
    _name = 'supplier.shortage.claim.dispute'
    _inherit = 'supplier.shortage.response.scope'
    _description = 'Supplier Dispute Case'
    _rec_name = 'issue_reference'
    _order = 'priority desc, next_action_date, id desc'
    evidence_ids = fields.Many2many('supplier.shortage.claim.evidence', 'ssc_dispute_evidence_rel', 'dispute_id', 'evidence_id', check_company=True)
    issue_reference = fields.Char(required=True, help='Stable issue reference, unique while active within a submitted revision.')
    claim_line_id = fields.Many2one('supplier.shortage.claim.line', check_company=True, ondelete='restrict')
    assigned_user_id = fields.Many2one('res.users', required=True, default=lambda s:s.env.user, check_company=True)
    priority = fields.Selection([('0','Normal'),('1','High'),('2','Urgent')], default='0', required=True)
    dispute_state = fields.Selection([('open','Open'),('evidence','Evidence Requested'),('investigation','Under Investigation'),('negotiating','Negotiating'),
        ('supplier','Awaiting Supplier'),('internal','Awaiting Internal Decision'),('resolved','Resolved'),('closed','Closed')], default='open', required=True, readonly=True, index=True)
    active_issue_key = fields.Char(readonly=True, index=True)
    next_action = fields.Char()
    next_action_date = fields.Date(string='Internal Follow-up Target')
    contractual_deadline = fields.Date()
    deadline_basis = fields.Text(help='Actual agreement/reference and calendar basis; never an invented contractual deadline.')
    resolution_summary = fields.Text()
    resolved_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    resolved_at = fields.Datetime(readonly=True)
    overdue = fields.Boolean(compute='_overdue')
    _issue_unique = models.Constraint('UNIQUE(active_issue_key)', 'This submitted revision already has an active dispute for this issue.')

    @api.depends('next_action_date','dispute_state')
    def _overdue(self):
        for rec in self:
            rec.overdue = bool(rec.next_action_date and rec.next_action_date<fields.Date.today() and rec.dispute_state not in ('resolved','closed'))

    @api.model_create_multi
    def create(self, values):
        allowed = {'submission_id','response_id','evidence_ids','description','issue_reference','claim_line_id','assigned_user_id','priority',
                   'next_action','next_action_date','contractual_deadline','deadline_basis'}
        if any(set(v)-allowed for v in values):
            raise AccessError('Dispute state, resolution and provenance are server controlled.')
        clean=[]
        for v in values:
            sub=self.env[SUBMISSION].browse(v.get('submission_id')); authority(sub,True); sub._lock()
            key='%s:%s:%s' % (sub.id,v.get('claim_line_id') or 0,(v.get('issue_reference') or '').strip().casefold())
            if self.search_count([('active_issue_key','=',key)]):
                raise ValidationError('An active dispute already covers this issue and line.')
            clean.append(dict(v,active_issue_key=key,recorded_by=self.env.uid,event_date=fields.Datetime.now()))
        records=super(Dispute,self.with_context({})).create(clean)
        for rec in records:
            rec._validate_scope(); rec._validate_case()
            rec.submission_id._audit('Dispute %s opened for line %s, assigned to user %s.' % (rec.id,rec.claim_line_id.id or '-',rec.assigned_user_id.id),rec.description)
            version_submission(rec.submission_id)
        return records

    def _validate_case(self):
        for rec in self:
            if rec.claim_line_id:
                rec.claim_line_id.check_access('read')
                if rec.claim_line_id.id not in {r['line_id'] for r in rec.submission_id.external_snapshot['lines']}:
                    raise ValidationError('Dispute line must belong to the submitted revision.')
            if rec.assigned_user_id.share or rec.company_id not in rec.assigned_user_id.company_ids:
                raise ValidationError('Assign an internal employee authorized for this company.')
            if rec.contractual_deadline and len((rec.deadline_basis or '').strip())<20:
                raise ValidationError('A contractual deadline needs a documented agreement and calendar basis.')

    def write(self, values):
        allowed={'assigned_user_id','priority','next_action','next_action_date','contractual_deadline','deadline_basis','resolution_summary'}
        if set(values)-allowed:
            raise AccessError('Use controlled dispute transitions; original scope and evidence are immutable.')
        for rec in self:
            authority(rec,True); rec.submission_id._lock()
            if rec.dispute_state=='closed':
                raise AccessError('Closed dispute history is immutable.')
        def differs(rec):
            for field,value in values.items():
                current=rec[field].id if rec._fields[field].type=='many2one' else rec[field]
                incoming=rec._fields[field].convert_to_cache(value,rec)
                if current!=incoming:
                    return True
            return False
        changed=self.filtered(differs)
        old={r.id:{f:str(r[f]) for f in values} for r in changed}
        result=super().write(values)
        self._validate_case()
        for rec in changed:
            rec.submission_id._audit('Dispute %s updated: %s -> %s' % (rec.id,old[rec.id],{f:str(rec[f]) for f in values}))
        return result

    def action_transition(self, state):
        self.ensure_one(); authority(self,True); self.submission_id._lock(); self.invalidate_recordset()
        allowed={'open':{'evidence','investigation','negotiating','supplier','internal','resolved'},'evidence':{'investigation','supplier','internal','resolved'},
                 'investigation':{'evidence','negotiating','supplier','internal','resolved'},'negotiating':{'evidence','supplier','internal','resolved'},
                 'supplier':{'evidence','investigation','negotiating','internal','resolved'},'internal':{'investigation','negotiating','supplier','resolved'},
                 'resolved':{'investigation','closed'},'closed':set()}
        if state == self.dispute_state:
            return True
        if state not in allowed[self.dispute_state]:
            raise ValidationError('Invalid dispute transition.')
        values={'dispute_state':state}
        if state in ('resolved','closed'):
            if len((self.resolution_summary or '').strip())<20:
                raise ValidationError('Document a business resolution before resolving or closing the dispute.')
            values.update(resolved_by=self.env.uid,resolved_at=fields.Datetime.now())
        if state=='closed':
            values['active_issue_key']=False
        previous=self.dispute_state
        super(Dispute,self).write(values)
        self.submission_id._audit('Dispute %s: %s -> %s. Financial claim lifecycle unchanged.' % (self.id,previous,state),self.resolution_summary or self.description)
        return True

    def action_investigate(self): return self.action_transition('investigation')
    def action_request_evidence(self): return self.action_transition('evidence')
    def action_negotiate(self): return self.action_transition('negotiating')
    def action_await_supplier(self): return self.action_transition('supplier')
    def action_escalate(self): return self.action_transition('internal')
    def action_resolve(self): return self.action_transition('resolved')
    def action_close(self): return self.action_transition('closed')
