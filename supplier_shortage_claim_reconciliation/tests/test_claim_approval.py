"""STEP 12: real non-superuser business actions, native sources and frozen history."""
from copy import deepcopy
from datetime import timedelta
from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user
from .test_claim_preparation import PreparationCase
from .preparation_fixtures import complete_evidence, additional_receipt, independent_checker

M='supplier_shortage_claim_reconciliation.'


class TestClaimApproval(PreparationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups=',purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly'
        cls.buyer=new_test_user(cls.env,login='ssc_approval_purchase',groups=M+'group_claim_manager'+groups,
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        cls.admin=new_test_user(cls.env,login='ssc_approval_config',groups=M+'group_module_admin'+groups,
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        for user in cls.reviewer|cls.finance:
            user.write({'group_ids':[Command.link(cls.env.ref('stock.group_stock_manager').id)]})
        cls.Policy=cls.env['supplier.shortage.approval.policy'].with_user(cls.admin)
        # Each test starts with explicit policy coverage, independent of disposable
        # fixtures from earlier concurrency runs. TransactionCase rolls this back.
        cls.Policy.search([('company_id','=',cls.company.id),('active','=',True)]).write({'active':False})

    def _policy(self, chain='purchase', **values):
        return self.Policy.create(dict({'name':'Company approval rule','company_id':self.company.id,'approval_chain':chain},**values))

    def _ready(self, review=True, responsible=None, order=None):
        claim=self._prepare(self._candidates(order=order))
        if responsible: claim.user_id=responsible
        claim.shortage_reason='Native receiving and posted bill discrepancy reviewed independently'
        complete_evidence(claim)
        claim.action_submit_for_review()
        if review: claim.with_user(self.reviewer).action_verify_financial_value('Posted bill untaxed price, discount and quantity reconciled')
        return claim

    def _request(self, chain='purchase'):
        policy=self._policy(chain)
        claim=self._ready()
        claim.action_request_internal_approval()
        return claim,policy

    def _bill(self, lines, posted=False, **kwargs):
        # One native invoice setup path; special pricing is used only by the tax test.
        bill=super()._bill(lines,False if getattr(self,'approval_tax',False) else posted,**kwargs)
        if getattr(self,'approval_tax',False):
            bill.invoice_line_ids.filtered(lambda l:l.display_type=='product').write({
                'price_unit':11,'discount':10,'tax_ids':[Command.set(self.approval_tax.ids)]})
            if posted: bill.action_post()
        return bill

    def _step(self,claim,role,user):
        return claim.approval_step_ids.filtered(lambda r:r.role==role and r.cycle_id.number==claim.approval_cycle).with_user(user)

    def test_policy_company_and_configuration_permissions(self):
        policy=self._policy()
        self.assertEqual(policy.currency_id,self.company.currency_id)
        with self.assertRaises(AccessError): policy.with_user(self.user).write({'maximum_amount':100})
        with self.assertRaises(AccessError): self.Policy.with_user(self.user).create({'name':'Forged','company_id':self.company.id})
        with self.assertRaises(AccessError): self._policy(company_id=self.other.id)

    def test_policy_negative_overlap_and_dates(self):
        for values in ({'minimum_amount':-1},{'maximum_amount':-1},{'minimum_amount':10,'maximum_amount':5},
                       {'effective_from':'2026-02-01','effective_to':'2026-01-01'}):
            with self.subTest(values=values),self.assertRaises(ValidationError),self.cr.savepoint(): self._policy(**values)
        self._policy(maximum_amount=100)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._policy(minimum_amount=99,maximum_amount=200)

    def test_exact_policy_boundaries_and_gap(self):
        low=self._policy(maximum_amount=100)
        medium=self._policy('accounts',minimum_amount=100,maximum_amount=500)
        high=self._policy('finance',minimum_amount=500)
        for amount,expected in [(0,low),(100,low),(100.01,medium),(500,medium),(500.01,high)]:
            self.assertEqual(self.Policy._select_for(self.company,amount,fields.Date.today()),expected)
        medium.active=False
        with self.assertRaises(ValidationError): self.Policy._select_for(self.company,200,fields.Date.today())
        with self.assertRaises(ValidationError): self.Policy._select_for(self.other,100,fields.Date.today())

    def test_missing_policy_atomic_and_provisional_not_verified(self):
        claim=self._ready(review=False)
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError): claim.action_request_internal_approval()
        self.assertFalse(claim.approval_cycle_ids)
        self.assertEqual(claim.audit_event_ids,before)
        claim.with_user(self.reviewer).action_verify_financial_value('Bill quantity and native untaxed subtotal checked')
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError): claim.action_request_internal_approval()
        self.assertEqual(claim.audit_event_ids,before)
        self.assertFalse(claim.approval_cycle_ids)

    def test_low_value_chain_and_no_source_side_effects(self):
        claim,policy=self._request()
        self.supplier.email='supplier-approval-test@example.invalid'
        claim.message_subscribe(partner_ids=claim.partner_id.ids)
        models=('account.move','account.move.line','account.payment','stock.move','stock.quant','mail.mail')
        counts={m:self.env[m].search_count([]) for m in models}
        source=(claim.purchase_id.write_date,claim.purchase_id.order_line.write_date,
                claim.purchase_id.picking_ids.mapped('write_date'),claim.purchase_id.order_line.invoice_lines.mapped('write_date'))
        stage=self._step(claim,'purchase',self.buyer)
        self.assertTrue(stage.can_decide)
        stage.action_approve()
        self.assertFalse(stage.can_decide)
        self.assertEqual(claim.approval_status,'approved')
        self.assertEqual(claim.state,'under_review')
        self.assertEqual(counts,{m:self.env[m].search_count([]) for m in models})
        self.assertEqual(source,(claim.purchase_id.write_date,claim.purchase_id.order_line.write_date,
            claim.purchase_id.picking_ids.mapped('write_date'),claim.purchase_id.order_line.invoice_lines.mapped('write_date')))
        self.assertEqual(claim.verified_amount,150)
        context=claim.approval_cycle_ids.snapshot['commercial_context']
        self.assertEqual(context['reason'],claim.shortage_reason)
        self.assertEqual(context['lines'][0]['claimed_quantity'],claim.claim_line_ids.proposed_qty)

    def test_medium_value_chain(self):
        claim,policy=self._request('accounts')
        self.assertEqual(len(claim.approval_step_ids),2)
        self._step(claim,'purchase',self.buyer).action_approve()
        self.assertEqual(claim.approval_status,'accounts')
        self._step(claim,'accounts',self.reviewer).action_approve()
        self.assertEqual(claim.approval_status,'approved')

    def test_high_value_chain_order_and_actor(self):
        claim,policy=self._request('finance')
        finance=self._step(claim,'finance',self.finance)
        with self.assertRaises(ValidationError): finance.action_approve()
        self._step(claim,'purchase',self.buyer).action_approve()
        with self.assertRaises(ValidationError): finance.action_approve()
        self._step(claim,'accounts',self.reviewer).action_approve()
        finance.action_approve()
        self.assertEqual(claim.approval_status,'approved')
        self.assertEqual(finance.decision_by,self.finance)
        self.assertTrue(finance.decision_at)

    def test_self_approval_maker_and_preparer(self):
        claim,policy=self._request()
        with self.assertRaises(AccessError): self._step(claim,'purchase',self.user).action_approve()
        self.assertEqual(claim.approval_status,'purchase')

    def test_group_overlap_cannot_approve_independent_stages(self):
        claim,policy=self._request('finance')
        self.buyer.write({'group_ids':[Command.link(self.env.ref(M+'group_finance_manager').id)]})
        self._step(claim,'purchase',self.buyer).action_approve()
        with self.assertRaises(AccessError): self._step(claim,'accounts',self.buyer).action_approve()
        self._step(claim,'accounts',self.reviewer).action_approve()
        with self.assertRaises(AccessError): self._step(claim,'finance',self.buyer).action_approve()

    def test_unauthorized_rpc_and_wrong_company(self):
        claim,policy=self._request()
        with self.assertRaises(AccessError): self._step(claim,'purchase',self.no_stock).action_approve()
        stranger=new_test_user(self.env,login='ssc_wrong_company_approval',groups=M+'group_claim_manager',
            company_id=self.other.id,company_ids=[Command.set(self.other.ids)])
        with self.assertRaises(AccessError): self._step(claim,'purchase',stranger).action_approve()
        with self.assertRaises(AccessError): self._step(claim,'purchase',self.buyer).with_context(allowed_company_ids=self.other.ids).action_approve()

    def test_direct_orm_import_copy_and_context_bypass_blocked(self):
        claim,policy=self._request()
        for values in ({'state':'internally_approved'},{'approval_status':'approved'},{'verified_amount':999},
                       {'approval_cycle':9},{'approval_step_ids':[Command.clear()]}):
            with self.subTest(values=values),self.assertRaises(AccessError): claim.with_context(internal_approval=True,skip_security=True).write(values)
        step=self._step(claim,'purchase',self.buyer)
        for action in (lambda:step.write({'state':'approved','decision_by':self.buyer.id}),lambda:step.unlink(),lambda:step.copy(),
            lambda:step.create({'cycle_id':step.cycle_id.id,'sequence':9,'role':'finance','state':'approved'}),
            lambda:step.cycle_id.write({'snapshot':{}}),lambda:step.cycle_id.unlink(),lambda:step.cycle_id.copy()):
            with self.assertRaises(AccessError): action()
        with self.assertRaises(AccessError): claim.with_context(default_approval_status='approved').copy()

    def test_financial_verifier_independent_and_notes_required(self):
        claim=self._ready(review=False)
        self.user.write({'group_ids':[Command.link(self.env.ref(M+'group_accounts_reviewer').id)]})
        with self.assertRaises(AccessError): claim.action_verify_financial_value('I checked my own claim')
        with self.assertRaises(ValidationError): claim.with_user(self.reviewer).action_verify_financial_value()
        self.assertFalse(claim.financial_reviewer_id)

    def test_missing_evidence_and_rejected_evidence_block(self):
        claim=self._prepare(self._candidates())
        with self.assertRaises(ValidationError): claim.action_request_internal_approval()
        self.assertFalse(claim.approval_cycle_ids)

    def test_rejection_reason_history_and_later_steps(self):
        claim,policy=self._request('finance')
        stage=self._step(claim,'purchase',self.buyer)
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError): stage.action_reject()
        self.assertEqual(before,claim.audit_event_ids)
        stage.action_reject('Commercial basis is not acceptable')
        self.assertEqual(stage.state,'rejected')
        self.assertEqual(claim.approval_status,'rejected')
        self.assertTrue(all(s.state=='superseded' for s in claim.approval_step_ids-stage))
        self.assertEqual(claim.claim_line_ids.allocation_state,'active')
        with self.assertRaises(ValidationError): stage.action_approve()

    def test_return_correction_new_cycle_retains_history(self):
        claim,policy=self._request('accounts')
        old=claim.approval_cycle_ids
        snapshot=deepcopy(old.snapshot)
        stage=self._step(claim,'purchase',self.buyer)
        with self.assertRaises(ValidationError): stage.action_return_for_correction()
        stage.action_return_for_correction('Please correct commercial explanation')
        with self.assertRaises(ValidationError): claim.action_return_to_draft()
        claim.action_start_approval_correction('Document corrected commercial explanation')
        claim.note='Clarified delivery terms'
        claim.action_submit_for_review()
        claim.with_user(self.reviewer).action_verify_financial_value('Untaxed bill pricing reconfirmed after correction')
        claim.action_request_internal_approval()
        self.assertEqual(claim.approval_cycle,2)
        self.assertEqual(old.snapshot,snapshot)
        self.assertEqual(old.state,'superseded')
        self.assertEqual(stage.state,'returned')
        with self.assertRaises(ValidationError): stage.action_approve()

    def test_policy_edits_do_not_rewrite_cycle_or_independence(self):
        claim,policy=self._request('accounts')
        old=deepcopy(claim.approval_cycle_ids.snapshot)
        policy.write({'approval_chain':'purchase','separate_approvers_required':False})
        self.assertEqual(old,claim.approval_cycle_ids.snapshot)
        self._step(claim,'purchase',self.buyer).action_approve()
        self.assertEqual(claim.approval_status,'accounts')

    def test_duplicate_request_and_decision_have_no_duplicate_events(self):
        claim,policy=self._request()
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError): claim.action_request_internal_approval()
        self.assertEqual(before,claim.audit_event_ids)
        stage=self._step(claim,'purchase',self.buyer)
        stage.action_approve()
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError): stage.action_approve()
        self.assertEqual(before,claim.audit_event_ids)
        self.assertEqual(len(claim.approval_step_ids),1)

    def test_material_receipt_change_stales_only_target_and_freezes_snapshot(self):
        claim,policy=self._request()
        self._step(claim,'purchase',self.buyer).action_approve()
        snapshot=deepcopy(claim.approval_cycle_ids.snapshot)
        other=self._ready()
        other.action_request_internal_approval()
        additional_receipt(claim.purchase_id,5)
        self.assertEqual(claim.approval_status,'stale')
        self.assertEqual(other.approval_status,'purchase')
        self.assertEqual(snapshot,claim.approval_cycle_ids.snapshot)
        self.assertEqual(claim.approval_step_ids.state,'approved')

    def test_bill_reversal_blocks_current_approval(self):
        claim,policy=self._request()
        bill=claim.purchase_id.order_line.invoice_lines.move_id
        bill.button_draft()
        self.assertEqual(claim.approval_status,'stale')
        with self.assertRaises(ValidationError): self._step(claim,'purchase',self.buyer).action_approve()

    def test_reassignment_authority_company_role_and_maker(self):
        claim,policy=self._request()
        stage=self._step(claim,'purchase',self.admin)
        with self.assertRaises(AccessError): stage.with_user(self.buyer).action_reassign(self.buyer.id,'Commercial reviewer available')
        with self.assertRaises(AccessError): stage.action_reassign(self.user.id,'Maker cannot receive assignment')
        with self.assertRaises(AccessError): stage.action_reassign(self.reviewer.id,'Accounts role is not purchase authority')
        stage.action_reassign(self.buyer.id,'Commercial reviewer available')
        self.assertEqual(stage.assigned_user_id,self.buyer)
        self.assertEqual(stage.state,'pending')
        self.multi.write({'group_ids':[Command.link(self.env.ref(M+'group_claim_manager').id)]})
        with self.assertRaises(AccessError): stage.with_user(self.multi).action_approve()
        stage.with_user(self.buyer).action_approve()

    def test_company_search_group_and_views(self):
        claim,policy=self._request()
        stage=self._step(claim,'purchase',self.buyer)
        data=stage._read_group([],['company_id'],['__count'])
        self.assertEqual({company.id for company,count in data},{self.company.id})
        self.assertEqual(len(stage.action_my_pending()['domain'])>0,True)
        for model in ('supplier.shortage.claim.approval','supplier.shortage.approval.policy','supplier.shortage.claim'):
            self.env[model].with_user(self.buyer if model!='supplier.shortage.approval.policy' else self.admin).get_views([(False,'form'),(False,'list')])
        self.assertTrue(stage.can_decide)
        self.assertFalse(stage.with_user(self.user).can_decide)

    def test_wizard_does_not_trust_forged_operation_or_target(self):
        claim,policy=self._request()
        wizard=self.env['supplier.shortage.approval.wizard'].with_user(self.no_stock).create({
            'step_id':claim.approval_step_ids.id,'operation':'reject','reason':'Forged unauthorized decision'})
        with self.assertRaises(AccessError): wizard.action_apply()
        with Form(self.env['supplier.shortage.approval.wizard'].with_user(self.reviewer).with_context(default_claim_id=claim.id,default_operation='verify')) as form:
            form.reason='Native posted invoice financial review'
        self.assertEqual(form.record.claim_id,claim)

    def test_responsible_preparer_is_also_excluded(self):
        self._policy()
        claim=self._ready(responsible=self.buyer)
        claim.action_request_internal_approval()
        with self.assertRaises(AccessError): self._step(claim,'purchase',self.buyer).action_approve()

    def test_currency_conversion_frozen_and_missing_rate_blocks(self):
        foreign=self.env['res.currency'].create({'name':'Q12','symbol':'Q','rounding':0.01})
        date=fields.Date.today()
        for currency,rate in [(self.company.currency_id,1),(foreign,0.5)]:
            self.env['res.currency.rate'].create({'currency_id':currency.id,'company_id':self.company.root_id.id,'name':date,'rate':rate})
        prior=self.env['res.currency.rate'].create({'currency_id':foreign.id,'company_id':self.company.root_id.id,
            'name':date-timedelta(days=1),'rate':0.25})
        order=self._order(100)
        order.currency_id=foreign
        self._policy(maximum_amount=200)
        self._policy('finance',minimum_amount=200)
        claim=self._ready(order=order)
        claim.action_request_internal_approval()
        cycle=claim.approval_cycle_ids
        self.assertEqual(cycle.currency_id,foreign)
        expected=foreign._convert(150,self.company.currency_id,self.company,date)
        self.assertEqual(cycle.comparison_amount,expected)
        source=next(row for row in cycle.conversion_basis['source_rates'] if row['currency_id']==foreign.id)
        self.assertEqual(source['id'],prior.id)
        self.assertEqual(source['date'],str(prior.name))
        self.assertEqual(claim.approval_status,'purchase')
        self.assertEqual(len(cycle.step_ids),3)
        frozen=deepcopy(cycle.conversion_basis)
        self.env['res.currency.rate'].search([('currency_id','=',foreign.id)]).write({'rate':0.125})
        self.assertEqual(cycle.conversion_basis,frozen)
        self._step(claim,'purchase',self.buyer).action_approve()
        self.assertEqual(cycle.comparison_amount,expected)
        # A distinct unsubmitted reviewed claim cannot use an absent rate fallback.
        # Exercise the conversion guard with a foreign claim already verified, before request.
        claim2_order=self._order(100)
        claim2_order.currency_id=foreign
        claim2=self._ready(order=claim2_order)
        self.env['res.currency.rate'].search([('currency_id','=',foreign.id)]).unlink()
        with self.assertRaises(ValidationError): claim2.action_request_internal_approval()
        self.assertFalse(claim2.approval_cycle_ids)
        self.env['res.currency.rate'].create({'currency_id':foreign.id,'company_id':self.company.root_id.id,
            'name':date+timedelta(days=1),'rate':0.5})
        with self.assertRaises(ValidationError): claim2.action_request_internal_approval()
        self.assertFalse(claim2.approval_cycle_ids)

    def test_changed_quantity_reselects_policy_in_new_cycle(self):
        self._policy(maximum_amount=100)
        self._policy('accounts',minimum_amount=100)
        claim=self._ready()
        claim.action_request_internal_approval()
        self.assertEqual(len(claim.approval_step_ids),2)
        old=claim.approval_cycle_ids
        self._step(claim,'purchase',self.buyer).action_return_for_correction('Correct eligible shortage quantity')
        claim.action_start_approval_correction('Quantity corrected against reserved source')
        claim.correction_reason='Reduce reserved claim quantity to five units'
        claim.claim_line_ids.proposed_qty=5
        claim.action_revalidate_preparation()
        complete_evidence(claim)
        claim.action_submit_for_review()
        with self.assertRaises(ValidationError): claim.action_request_internal_approval()
        claim.with_user(self.reviewer).action_verify_financial_value('Corrected quantity and untaxed pricing reverified')
        claim.action_request_internal_approval()
        latest=claim.approval_cycle_ids.filtered(lambda c:c.number==2)
        self.assertEqual(latest.amount,50)
        self.assertEqual(len(latest.step_ids),1)
        self.assertEqual(old.amount,150)

    def test_rejected_supporting_evidence_blocks_resubmission(self):
        claim,policy=self._request()
        self._step(claim,'purchase',self.buyer).action_return_for_correction('Correct disputed accounting evidence')
        claim.action_start_approval_correction('Accounting proof requires correction')
        evidence=claim.evidence_ids.filtered(lambda e:e.evidence_type=='bill')
        checker=independent_checker(self.env,self.company)
        evidence.with_user(checker).write({'verification_note':'Accounting reference needs correction'})
        evidence.with_user(checker).action_reject()
        self.assertEqual(evidence.effective_state,'rejected')
        with self.assertRaises(ValidationError): claim.action_submit_for_review()
        self.assertEqual(claim.state,'draft')

    def test_accounts_crud_does_not_grant_preparation_authority(self):
        claim=self._prepare(self._candidates())
        with self.assertRaises(AccessError): claim.with_user(self.reviewer).write({'note':'Unauthorized preparation'})
        with self.assertRaises(AccessError): claim.claim_line_ids.with_user(self.reviewer).write({'proposed_qty':1})

    def test_multi_record_decisions_cannot_skip_stages(self):
        claim,policy=self._request('finance')
        with self.assertRaises(ValueError): claim.approval_step_ids.with_user(self.buyer).action_approve()
        self.assertTrue(all(s.state in ('waiting','pending') for s in claim.approval_step_ids))

    def test_ambiguous_split_bill_is_held_for_accounts(self):
        order=self._order(100)
        self._bill([(order.order_line,50)],True)
        claim=self._ready(order=order,review=False)
        with self.assertRaises(ValidationError): claim.with_user(self.reviewer).action_verify_financial_value('Multiple invoice lines require reconciliation')
        self.assertFalse(claim.financial_reviewer_id)
        self.assertFalse(claim.verified_basis)

    def test_tax_inclusive_discounted_bill_uses_native_untaxed_value(self):
        group=self.env['account.tax.group'].create({'name':'Approval tax','company_id':self.company.id})
        self.approval_tax=self.env['account.tax'].create({'name':'Included purchase tax','company_id':self.company.id,
            'type_tax_use':'purchase','amount':10,'amount_type':'percent','tax_group_id':group.id,'price_include_override':'tax_included'})
        claim=self._ready()
        self.assertEqual(claim.verified_amount,135)
        bill=claim.purchase_id.order_line.invoice_lines.filtered(lambda l:l.display_type=='product')
        self.assertEqual(bill.price_subtotal,900)
        self.assertNotEqual(claim.verified_amount,claim.claim_line_ids.submitted_untaxed_amount)

    def test_authorized_revocation_of_final_approval_requires_fresh_review(self):
        claim,policy=self._request()
        self._step(claim,'purchase',self.buyer).action_approve()
        snapshot=deepcopy(claim.approval_cycle_ids.snapshot)
        with self.assertRaises(AccessError): claim.with_user(self.reviewer).action_start_approval_correction('Correct the reviewed commercial basis')
        with self.assertRaises(AccessError): claim.action_start_approval_correction()
        claim.action_start_approval_correction('Revoke authorization for a documented quantity correction')
        self.assertEqual(claim.state,'draft')
        self.assertEqual(claim.approval_status,'stale')
        self.assertFalse(claim.verified_basis)
        self.assertEqual(claim.approval_step_ids.state,'approved')
        self.assertEqual(snapshot,claim.approval_cycle_ids.snapshot)
