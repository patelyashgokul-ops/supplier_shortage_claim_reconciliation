import base64

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from .preparation_fixtures import additional_receipt, independent_checker
from .test_replacement_settlement import TestReplacementSettlement as GoodsFixtures
from .test_supplier_submission import M, SupplierSubmissionCase

CREDIT='supplier.shortage.credit.allocation'


class TestCreditSettlement(SupplierSubmissionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.maker=new_test_user(cls.env,login='ssc_credit_maker',
            groups=M+'group_claim_user,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        cls.warehouse=independent_checker(cls.env,cls.company)

    def _credit(self,claim,qty,posted=True,**kwargs):
        return self._bill([(claim.claim_line_ids.purchase_line_id,qty)],posted,move_type='in_refund',**kwargs)

    def _proposal(self,claim,credit,amount,**extra):
        return self.env[CREDIT].with_user(self.maker).create({
            'claim_line_id':claim.claim_line_ids.id,'credit_line_id':credit.invoice_line_ids.id,
            'purpose':'shortage','source_amount':amount,'note':'Vendor confirmed credit for this specific underdelivery.',
            **extra})

    def _review(self,row):
        row.action_submit()
        row.with_user(self.reviewer).action_accounts()
        return row.with_user(self.finance)

    def test_posting_purpose_tax_and_payable_realization(self):
        claim,_=self._approved()
        credit=self._credit(claim,5,False)
        tax=self.env['account.tax'].create({'name':'Fixture GST 18%','amount':18,'amount_type':'percent',
                                           'type_tax_use':'purchase','company_id':self.company.id})
        credit.invoice_line_ids.tax_ids=[Command.set(tax.ids)]
        row=self._proposal(claim,credit,50)
        row.action_submit()
        self.assertEqual(row.state,'posting')
        self.assertEqual(row.effective_value,0)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            row.with_user(self.reviewer).action_accounts()
        credit.action_post()
        row.with_user(self.reviewer).action_accounts()
        finance=row.with_user(self.finance)
        snapshot=credit.invoice_line_ids.read(['balance','price_subtotal','price_total','amount_currency'])
        count=self.env['account.move'].search_count([])
        finance.action_verify()
        finance.action_verify()
        self.assertEqual(row.effective_value,50)
        self.assertEqual(row.source_tax,9)
        self.assertEqual(row.claim_quantity,0)
        self.assertEqual(row.realization,'open')
        self.assertEqual(row.available_source,0)
        self.assertEqual(credit.invoice_line_ids.read(list(snapshot[0].keys()-{'id'})),snapshot)
        self.assertEqual(self.env['account.move'].search_count([]),count)
        self.assertEqual(len(row.audit_ids.filtered(lambda event:event.change_summary=='Vendor credit recovery verified')),1)
        (credit | row.original_bill_id).line_ids.filtered(
            lambda item:item.account_id.account_type=='liability_payable').reconcile()
        self.assertEqual(row.realization,'payable')
        self.assertEqual(row.effective_value,50)
        self.assertEqual(claim.verified_recovery_amount,50)

    def test_shared_credit_capacity_and_multiple_notes(self):
        candidate=self._candidates()
        first,_=self._approved(candidates=candidate,quantity=8)
        second,_=self._approved(candidates=candidate,quantity=7)
        credit=self._credit(first,8)
        a=self._review(self._proposal(first,credit,50))
        a.action_verify()
        over=self._review(self._proposal(second,credit,40))
        with self.assertRaisesRegex(ValidationError,'capacity'),self.env.cr.savepoint():
            over.action_verify()
        self.assertFalse(over.verified_at)
        b=self._review(self._proposal(second,credit,30))
        b.action_verify()
        self.assertEqual(b.available_source,0)
        later=self._review(self._proposal(second,self._credit(second,3),25))
        later.action_verify()
        self.assertEqual(second.claim_line_ids.credit_value,55)
        self.assertEqual(second.claim_line_ids.settlement_unresolved_value,15)
        self.assertEqual(first.claim_line_ids.settlement_unresolved_value,30)

    def test_partial_and_full_native_reversal(self):
        claim,_=self._approved()
        credit=self._credit(claim,8)
        row=self._review(self._proposal(claim,credit,80))
        row.action_verify()
        correction=credit._reverse_moves([{'invoice_date':credit.invoice_date,'date':credit.date}])
        correction.invoice_line_ids.quantity=3
        correction.action_post()
        self.assertEqual(row.source_status,'partial')
        self.assertEqual(row.effective_value,0)
        self.assertTrue(claim.claim_line_ids.settlement_review)
        row.reason='Native correction removed three units from the original credit.'
        row.action_reverse()
        row.action_reverse()
        self.assertEqual(row.reversal_ids,correction)
        self.assertEqual(row.claim_value,80)
        retained=self._review(self._proposal(claim,credit,50))
        retained.action_verify()
        self.assertEqual(retained.effective_value,50)
        rest=credit._reverse_moves([{'invoice_date':credit.invoice_date,'date':credit.date}])
        rest.invoice_line_ids.quantity=5
        rest.action_post()
        self.assertEqual(retained.source_status,'reversed')
        self.assertEqual(retained.effective_value,0)
        self.assertEqual(claim.claim_line_ids.settlement_unresolved_value,150)

    def test_goods_credit_capacity_and_frozen_claim(self):
        claim,promise=GoodsFixtures._promised(self)
        approved=claim.verified_amount
        goods=GoodsFixtures._draft(self,claim,promise,additional_receipt(claim.purchase_id,6).move_ids,6)
        goods.action_request_verification()
        goods.with_user(self.warehouse).action_warehouse_verify()
        goods.with_user(self.finance).action_verify()
        credit=self._credit(claim,12)
        over=self._review(self._proposal(claim,credit,100))
        with self.assertRaisesRegex(ValidationError,'Combined goods'),self.env.cr.savepoint():
            over.action_verify()
        valid=self._review(self._proposal(claim,credit,60,source_quantity=6))
        valid.action_verify()
        self.assertEqual(goods.effective_value,60)
        self.assertEqual(valid.claim_quantity,6)
        self.assertEqual(claim.claim_line_ids.settlement_unresolved_value,30)
        self.assertEqual(claim.verified_amount,approved)

    def test_source_authorization_and_forged_wizard(self):
        claim,_=self._approved()
        wrong=self._bill([(self._order(10).order_line,2)],True,move_type='in_refund')
        with self.assertRaises(ValidationError),self.env.cr.savepoint():
            self._proposal(claim,wrong,20)
        wrong_company=self._bill([(self._order(10,warehouse=self.other_wh).order_line,2)],True,move_type='in_refund')
        wizard=self.env['supplier.shortage.credit.matching'].with_user(self.maker).create({
            'claim_line_id':claim.claim_line_ids.id,'credit_line_id':wrong_company.invoice_line_ids.id,'source_amount':20,
            'note':'This forged wizard requests a credit belonging to an unrelated company.'})
        with self.assertRaises((AccessError,ValidationError)),self.env.cr.savepoint():
            wizard.action_propose()
        self.assertFalse(self.env[CREDIT].with_user(self.maker).search([('credit_move_id','=',wrong_company.id)]))
        credit=self._credit(claim,5)
        row=self._proposal(claim,credit,25,purpose='rebate')
        row.action_submit()
        with self.assertRaisesRegex(ValidationError,'shortage-resolution'),self.env.cr.savepoint():
            row.with_user(self.reviewer).action_accounts()
        with self.assertRaises(AccessError),self.env.cr.savepoint():
            row.action_verify()
        with self.assertRaises(AccessError),self.env.cr.savepoint():
            row.write({'state':'verified'})
        canceled=self._credit(claim,2,False)
        proposed=self._proposal(claim,canceled,10)
        canceled.button_cancel()
        self.assertEqual(proposed.source_status,'cancel')
        self.assertEqual(proposed.effective_value,0)

    def test_dated_foreign_currency_snapshot(self):
        claim,_=self._approved()
        other=self.env['res.currency'].create({'name':'XSC','symbol':'XC','rounding':0.01,'active':True})
        credit=self._credit(claim,8,False,currency=other)
        credit.action_post()
        row=self._proposal(claim,credit,20)
        row.action_submit()
        with self.assertRaisesRegex(ValidationError,'historical FX'),self.env.cr.savepoint():
            row.with_user(self.reviewer).action_accounts()
        rate=self.env['res.currency.rate'].create({'currency_id':other.id,'company_id':self.company.root_id.id,
                                             'name':credit.date,'rate':2})
        row.with_user(self.reviewer).action_accounts()
        row.with_user(self.finance).action_verify()
        self.assertEqual(row.source_currency_id,other)
        self.assertEqual(row.currency_id,claim.currency_id)
        self.assertEqual(row.effective_value,other._convert(20,claim.currency_id,self.company,credit.date))
        self.assertEqual(row.company_value,self.company.currency_id.round(abs(credit.invoice_line_ids.balance)/4))
        self.assertEqual(row.fx_basis['rates'][0]['date'],str(credit.date))
        historical=row.claim_value
        rate.rate=3
        self.assertTrue(row.source_dirty)
        self.assertEqual(row.effective_value,0)
        self.assertEqual(row.claim_value,historical)

    def test_manual_matching_and_bank_realization_of_existing_credit(self):
        claim,_,submission,_=self._queued()
        self.assertEqual(self._send_mocked(submission),1)
        line=claim.claim_line_ids
        credit=self._credit(claim,5,False)
        credit.invoice_line_ids.purchase_line_id=False
        credit.action_post()
        bare=self._proposal(claim,credit,50)
        bare.action_submit()
        with self.assertRaisesRegex(ValidationError,'independently verified evidence'),self.env.cr.savepoint():
            bare.with_user(self.reviewer).action_accounts()
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(self.maker).create({
            'claim_id':claim.id,'supplier_response_submission_id':submission.id,
            'scope_line_ids':[Command.set(line.ids)],'evidence_type':'supplier_confirmation',
            'description':'Supplier sent exact credit attribution for the documented claim and vendor credit source.',
            'filename':'attribution.txt','upload_data':base64.b64encode(b'Vendor confirms this credit applies to the specific shortage claim.'),
            'verification_note':'The supplier, document reference and exact claim scope have been checked.',
        })
        from .preparation_fixtures import verify_document
        verify_document(evidence)
        wizard=self.env['supplier.shortage.credit.matching'].with_user(self.maker).create({
            'claim_line_id':line.id,'credit_line_id':credit.invoice_line_ids.id,
            'purpose':'shortage','source_amount':50,'evidence_id':evidence.id,
            'note':'Supplier attributed the posted credit to this exact line of the submitted shortage.',
        })
        action=wizard.action_propose()
        row=self.env[CREDIT].with_user(self.maker).browse(action['res_id'])
        self.assertEqual(row.state,'draft')
        self._review(row).action_verify()
        self.assertEqual(row.realization,'open')
        bank=self.env['account.journal'].create({
            'name':'Disposable supplier refund bank','code':'S16B','type':'bank','company_id':self.company.id})
        statement=self.env['account.bank.statement.line'].create({
            'payment_ref':'Independent bank confirmation of the existing supplier credit',
            'date':credit.date,'journal_id':bank.id,'partner_id':self.supplier.id,
            'amount':50,'counterpart_account_id':self.accounts[self.company.id]['payable'].id,
        })
        self.assertEqual(statement.move_id.state,'posted')
        (credit | statement.move_id).line_ids.filtered(
            lambda item:item.account_id.account_type=='liability_payable').reconcile()
        self.assertEqual(row.realization,'cash')
        self.assertEqual(row.effective_value,50)
        self.assertEqual(claim.verified_recovery_amount,50)

    def test_draft_source_line_removal_preserves_proposal_history(self):
        claim=self._claim(self._order(10))
        credit=self._credit(claim,2,False)
        row=self._proposal(claim,credit,20)
        move_line_id=credit.invoice_line_ids.id
        row.action_submit()
        credit.invoice_line_ids.unlink()
        self.assertFalse(row.credit_line_id)
        self.assertEqual(row.source_status,'review')
        self.assertEqual(row.effective_value,0)
        self.assertIn(move_line_id,[event.valuation_basis.get('credit_line') for event in row.audit_ids])
