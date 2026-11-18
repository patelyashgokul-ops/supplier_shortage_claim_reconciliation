import base64

from odoo import Command
from odoo.exceptions import AccessError, ValidationError

from .preparation_fixtures import verify_document
from .test_credit_settlement import CreditSettlementCase


CASH='supplier.shortage.cash.settlement'


class TestCashSettlement(CreditSettlementCase):

    def _bank(self,claim,amount,account,**kwargs):
        company=claim.company_id
        journal=self.env['account.journal'].search([('company_id','=',company.id),('code','=','S17B')],limit=1)
        journal=journal or self.env['account.journal'].create({
            'name':'Disposable refund bank','code':'S17B','type':'bank','company_id':company.id})
        return self.env['account.bank.statement.line'].create({
            'payment_ref':'Verified supplier remittance fixture','journal_id':journal.id,
            'partner_id':claim.partner_id.id,'date':claim.claim_date,'amount':amount,
            'counterpart_account_id':account.id,**kwargs,
        })

    def _cash(self,claim,source,amount,kind='direct',bank=None,**extra):
        return self.env[CASH].with_user(self.maker).create({
            'claim_line_id':claim.claim_line_ids.id,'source_line_id':source.id,'kind':kind,
            'bank_id':bank.id if bank else False,'gross_amount':amount,
            'untaxed_amount':amount if kind=='direct' else 0,
            'note':'Independently attributed supplier settlement for the specific original claim line.',**extra,
        })

    def _direct_evidence(self):
        claim,_,submission,_=self._queued()
        self.assertEqual(self._send_mocked(submission),1)
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(self.maker).create({
            'claim_id':claim.id,'supplier_response_submission_id':submission.id,
            'scope_line_ids':[Command.set(claim.claim_line_ids.ids)],'evidence_type':'supplier_confirmation',
            'description':'Supplier confirms standalone shortage refund and its untaxed basis.',
            'filename':'refund.txt','upload_data':base64.b64encode(b'Supplier identifies the independent refund and claim line.'),
            'verification_note':'Independently checked exact remittance, attribution and untaxed settlement terms.',
        })
        verify_document(evidence)
        return claim,evidence

    def test_direct_partial_refunds_and_shared_claim_capacity(self):
        claim,evidence=self._direct_evidence()
        account=self.accounts[claim.company_id.id]['expense']
        first=self._bank(claim,70,account)
        second=self._bank(claim,100,account)
        a=self._cash(claim,first._seek_for_lines()[2],70,bank=first,evidence_id=evidence.id)
        self.assertEqual(a.effective_value,0)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            a.write({'state':'verified'})
        native=first.move_id.line_ids.read(['balance','amount_currency'])
        documents=self.env['account.move'].search_count([])
        self._review(a).action_verify()
        duplicate=self._review(self._cash(claim,first._seek_for_lines()[2],25,bank=first,evidence_id=evidence.id))
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            duplicate.action_verify()
        over=self._review(self._cash(claim,second._seek_for_lines()[2],90,bank=second,evidence_id=evidence.id))
        with self.assertRaisesRegex(ValidationError,'approved claim capacity'), self.env.cr.savepoint():
            over.action_verify()
        b=self._review(self._cash(claim,second._seek_for_lines()[2],30,bank=second,evidence_id=evidence.id))
        b.action_verify()
        self.assertEqual(claim.verified_recovery_amount,100)
        self.assertEqual(claim.claim_line_ids.settlement_unresolved_value,50)
        self.assertEqual(first.move_id.line_ids.read(['balance','amount_currency']),native)
        self.assertEqual(self.env['account.move'].search_count([]),documents)
        correction=second.move_id._reverse_moves([{'date':second.date}])
        correction.action_post()
        self.assertEqual(b.effective_value,0)
        self.assertEqual(claim.claim_line_ids.settlement_unresolved_value,80)
        b.reason='The posted accounting reversal withdrew the verified independent supplier refund.'
        b.action_reverse()
        self.assertEqual(b.reversal_ids,correction)
        self.assertEqual(b.claim_value,30)
        unrelated=self._claim(self._order(10,warehouse=self.other_wh),user=self.multi)
        foreign_bank=self._bank(unrelated,10,self.accounts[self.other.id]['expense'])
        with self.assertRaises((AccessError,ValidationError)),self.env.cr.savepoint():
            self._cash(claim,foreign_bank._seek_for_lines()[2],10,bank=foreign_bank,evidence_id=evidence.id)

    def test_bank_and_payable_realization_are_not_additional_recovery(self):
        claim,_=self._approved()
        payable=self.accounts[claim.company_id.id]['payable']
        credit_doc=self._credit(claim,10)
        credit=self._review(self._proposal(claim,credit_doc,100))
        credit.action_verify()
        statement=self._bank(claim,60,payable)
        bank_payable=statement._seek_for_lines()[2]
        credit_payable=credit_doc.line_ids.filtered(lambda item:item.account_id==payable)
        forged=self._cash(claim,bank_payable,60,bank=statement)
        forged.action_submit()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            forged.with_user(self.reviewer).action_accounts()
        refund=self._cash(claim,bank_payable,60,kind='credit',bank=statement,credit_id=credit.id)
        refund.action_submit()
        with self.assertRaisesRegex(ValidationError,'native payable match'), self.env.cr.savepoint():
            refund.with_user(self.reviewer).action_accounts()
        (bank_payable | credit_payable).reconcile()
        refund.with_user(self.reviewer).action_accounts()
        refund.with_user(self.finance).action_verify()
        bill_payable=credit.original_bill_id.line_ids.filtered(lambda item:item.account_id==payable)
        self.env['account.move.line'].browse((bill_payable | credit_payable).ids).reconcile()
        offset=self._review(self._cash(claim,bill_payable,40,kind='offset',credit_id=credit.id))
        offset.action_verify()
        line=claim.claim_line_ids
        self.assertEqual((line.credit_value,line.cash_value,line.cash_realization,line.payable_realization),(100,0,60,40))
        self.assertEqual(claim.verified_recovery_amount,100)
        ledger=self.env['supplier.shortage.recovery.ledger'].with_user(self.finance).search([('claim_id','=',claim.id)])
        self.assertEqual(sum(ledger.mapped('effective_value')),100)
        self.assertEqual(sum(ledger.mapped('realization_value')),100)
        self.assertTrue(ledger.filtered(lambda item:item.cash_id==offset).action_open_source()['res_id'])
        bank_payable.remove_move_reconcile()
        self.assertEqual(refund.realized_value,0)
        self.assertEqual(offset.realized_value,40)
        self.assertEqual(credit.effective_value,100)
        self.assertEqual(claim.verified_recovery_amount,100)
        refund=refund.with_user(self.finance)
        refund.reason='The bank reconciliation was removed, so this realization is withdrawn.'
        refund.action_reverse()
        self.assertEqual(refund.realized_snapshot,60)
        self.assertTrue(refund.audit_ids)

    def test_inbound_payment_requires_its_native_bank_match(self):
        claim,_=self._approved()
        credit_doc=self._credit(claim,6)
        credit=self._review(self._proposal(claim,credit_doc,60))
        credit.action_verify()
        company=claim.company_id
        clearing=self.env['account.account'].create({
            'name':'Disposable incoming payment clearing','code':'S17C','account_type':'asset_current',
            'reconcile':True,'company_ids':[Command.set(company.ids)]})
        statement=self._bank(claim,60,clearing)
        method=statement.journal_id.inbound_payment_method_line_ids[:1]
        method.payment_account_id=clearing
        payment=self.env['account.payment'].create({
            'company_id':company.id,'journal_id':statement.journal_id.id,'payment_method_line_id':method.id,
            'partner_type':'supplier','payment_type':'inbound','partner_id':claim.partner_id.id,
            'amount':60,'date':statement.date,'currency_id':statement.currency_id.id,
        })
        payment.action_post()
        liquidity,payable,_=payment._seek_for_lines()
        self.assertEqual(payment.move_id.state,'posted')
        credit_payable=credit_doc.line_ids.filtered(lambda line:line.account_id==payable.account_id)
        (credit_payable | payable).reconcile()
        row=self._cash(claim,payable,60,kind='credit',bank=statement,payment_id=payment.id,credit_id=credit.id)
        row.action_submit()
        self.assertEqual(row.source_status,'available')
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            row.with_user(self.reviewer).action_accounts()
        bank_counterpart=statement._seek_for_lines()[2]
        (liquidity | bank_counterpart).reconcile()
        row.with_user(self.reviewer).action_accounts()
        row.with_user(self.finance).action_verify()
        self.assertEqual(row.source_status,'bank')
        self.assertEqual(row.realized_value,60)
        self.assertEqual(row.effective_value,0)
        bank_counterpart.remove_move_reconcile()
        self.assertEqual(row.realized_value,0)
        self.assertEqual(claim.verified_recovery_amount,60)

    def test_direct_foreign_currency_and_rate_amendment(self):
        claim,evidence=self._direct_evidence()
        currency=self.env['res.currency'].create({'name':'XSR','symbol':'XR','rounding':0.01})
        rate=self.env['res.currency.rate'].create({
            'currency_id':currency.id,'company_id':claim.company_id.root_id.id,'name':claim.claim_date,'rate':2})
        bank=self._bank(claim,50,self.accounts[claim.company_id.id]['expense'],
                        foreign_currency_id=currency.id,amount_currency=100)
        row=self._cash(claim,bank._seek_for_lines()[2],80,bank=bank,evidence_id=evidence.id,untaxed_amount=70)
        self._review(row).action_verify()
        self.assertEqual(row.source_currency_id,currency)
        value=currency._convert(70,claim.currency_id,claim.company_id,bank.date)
        self.assertEqual(row.effective_value,value)
        self.assertEqual(row.fx_basis['rates'][0]['id'],rate.id)
        rate.rate=3
        self.assertTrue(row.source_dirty)
        self.assertEqual(row.effective_value,0)
        self.assertEqual(row.claim_value,value)
