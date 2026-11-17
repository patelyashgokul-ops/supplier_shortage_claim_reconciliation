"""Access-aware revision-specific supplier positions on the existing claim form."""
from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError
from .supplier_response import RESPONSE
from .claim_submission import SUBMISSION

POSITION_OUTPUTS={'response_ids','dispute_ids','negotiation_ids','commitment_ids','response_submission_id','latest_verified_response_id',
    'supplier_response_status','supplier_approved_amount','supplier_accepted_amount','supplier_rejected_amount','supplier_disputed_amount',
    'supplier_pending_amount','current_offer_id','promised_amount','supplier_financial_review','verified_recovery_amount','open_dispute_count',
    'next_supplier_followup','last_supplier_response'}


class ClaimSupplierPosition(models.Model):
    _inherit = 'supplier.shortage.claim'
    response_ids = fields.One2many(RESPONSE,'claim_id',readonly=True,copy=False)
    dispute_ids = fields.One2many('supplier.shortage.claim.dispute','claim_id',readonly=True,copy=False)
    negotiation_ids = fields.One2many('supplier.shortage.claim.negotiation','claim_id',readonly=True,copy=False)
    commitment_ids = fields.One2many('supplier.shortage.claim.commitment','claim_id',readonly=True,copy=False)
    response_submission_id = fields.Many2one(SUBMISSION,compute='_supplier_position')
    latest_verified_response_id = fields.Many2one(RESPONSE,compute='_supplier_position')
    supplier_response_status = fields.Selection([('pending','Awaiting Supplier'),('acknowledged','Acknowledged'),('accepted','Fully Accepted'),
        ('partial','Partially Accepted'),('rejected','Fully Rejected'),('mixed','Mixed / Disputed'),('review','Requires Revalidation')],compute='_supplier_position')
    supplier_approved_amount = fields.Monetary(compute='_supplier_position',string='Submitted Approved Amount')
    supplier_accepted_amount = fields.Monetary(compute='_supplier_position')
    supplier_rejected_amount = fields.Monetary(compute='_supplier_position')
    supplier_disputed_amount = fields.Monetary(compute='_supplier_position')
    supplier_pending_amount = fields.Monetary(compute='_supplier_position')
    current_offer_id = fields.Many2one('supplier.shortage.claim.negotiation',compute='_supplier_position')
    promised_amount = fields.Monetary(compute='_supplier_position',help='One unambiguous current promise in claim currency. Multiple potentially overlapping promises require review and are not added.')
    supplier_financial_review = fields.Boolean(compute='_supplier_position')
    verified_recovery_amount = fields.Monetary(compute='_supplier_position',help='No verified settlement allocation source exists in Step 14; recovery remains zero.')
    open_dispute_count = fields.Integer(compute='_supplier_position')
    next_supplier_followup = fields.Date(compute='_supplier_position')
    last_supplier_response = fields.Datetime(compute='_supplier_position')

    @api.depends_context('uid','allowed_company_ids')
    def _supplier_position(self):
        # Batch searches, native access domains and prefetch. Never elevate financial reads.
        submissions=self.env[SUBMISSION].search([('claim_id','in',self.ids),('state','=','sent')],order='revision desc,id desc')
        responses=self.env[RESPONSE].search([('claim_id','in',self.ids)],order='id desc')
        offers=self.env['supplier.shortage.claim.negotiation'].search([('claim_id','in',self.ids)],order='id desc')
        promises=self.env['supplier.shortage.claim.commitment'].search([('claim_id','in',self.ids),('status','in',('promised','pending'))])
        disputes=self.env['supplier.shortage.claim.dispute'].search([('claim_id','in',self.ids),('dispute_state','not in',('resolved','closed'))])
        hidden=Domain('id','in',self.ids)
        hidden &= Domain.OR([Domain(f,'any!',~Domain('id','access','read')) for f in ('response_ids','dispute_ids','negotiation_ids','commitment_ids')])
        if self.search_count(hidden):
            raise AccessError('Supplier position is unavailable because some underlying decision records are restricted.')
        by_claim={c.id:dict(sub=[],response=[],offer=[],promise=[],dispute=[]) for c in self}
        for kind,records in [('sub',submissions),('response',responses),('offer',offers),('promise',promises),('dispute',disputes)]:
            for rec in records:
                by_claim[rec.claim_id.id][kind].append(rec)
        for claim in self:
            rows=by_claim[claim.id]
            sub=rows['sub'][0] if rows['sub'] else self.env[SUBMISSION]
            current=[r for r in rows['response'] if r.submission_id==sub and r.current_key and r.verification_state=='verified']
            verified=[r for r in rows['response'] if r.submission_id==sub and r.verification_state=='verified']
            pos=current[0] if current else self.env[RESPONSE]
            claim.response_submission_id=sub
            claim.latest_verified_response_id=verified[0] if verified else False
            amount=sub.amount if sub else 0
            claim.supplier_approved_amount=amount
            claim.supplier_accepted_amount=pos.accepted_amount if pos else 0
            claim.supplier_rejected_amount=pos.rejected_amount if pos else 0
            claim.supplier_disputed_amount=pos.disputed_amount if pos else 0
            claim.supplier_pending_amount=pos.pending_amount if pos else amount
            claim.verified_recovery_amount=0
            status='pending'
            if verified: status='acknowledged'
            if pos:
                if sub.currency_id.is_zero(amount-pos.accepted_amount): status='accepted'
                elif pos.accepted_amount>0: status='partial'
                elif sub.currency_id.is_zero(amount-pos.rejected_amount): status='rejected'
                else: status='mixed'
            if sub and (sub.superseded or claim.approval_status!='approved' or sub.approval_id.number!=claim.approval_cycle): status='review'
            claim.supplier_response_status=status
            supplier_offers=[o for o in rows['offer'] if o.submission_id==sub and o.negotiation_type in ('supplier_offer','revised','final')]
            claim.current_offer_id=supplier_offers[0] if supplier_offers else False
            same_currency=[p for p in rows['promise'] if p.submission_id==sub and p.currency_id==sub.currency_id]
            claim.promised_amount=same_currency[0].committed_amount if len(same_currency)==1 else 0
            claim.supplier_financial_review=len(same_currency)>1 or any(o.financial_review_required for o in supplier_offers[:1]) or any(p.currency_id!=sub.currency_id for p in rows['promise'] if p.submission_id==sub)
            claim.open_dispute_count=len(rows['dispute'])
            dates=[d.next_action_date for d in rows['dispute'] if d.next_action_date]
            claim.next_supplier_followup=min(dates) if dates else False
            claim.last_supplier_response=rows['response'][0].response_date if rows['response'] else False

    @api.model_create_multi
    def create(self, values):
        protected=POSITION_OUTPUTS
        if any(set(v)&protected for v in values) or any('default_'+f in self.env.context for f in protected):
            raise AccessError('Supplier case records require their controlled recording workflows.')
        return super().create(values)

    def write(self, values):
        if POSITION_OUTPUTS & set(values):
            raise AccessError('Supplier case history cannot be reassigned through claims.')
        return super().write(values)
