from odoo import api, models
from odoo.exceptions import AccessError, ValidationError


CLAIM_MODELS = {'supplier.shortage.claim', 'supplier.shortage.claim.line', 'supplier.shortage.claim.audit', 'supplier.shortage.claim.evidence'}


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model
    def _claim_document(self, model, record_id, operation):
        if model not in CLAIM_MODELS or not record_id:
            return self.env['supplier.shortage.claim']
        record = self.env[model].browse(record_id)
        record.check_access(operation)
        return record if model == 'supplier.shortage.claim' else record.claim_id

    @api.model
    def _validate_claim_attachment(self, model, record_id, vals):
        if model not in CLAIM_MODELS or not record_id:
            return
        if model == 'supplier.shortage.claim.audit':
            raise AccessError(self.env._('Formal audit events do not accept editable attachments.'))
        if model == 'supplier.shortage.claim.evidence':
            evidence = self.env[model].browse(record_id)
            evidence._require_edit()
            claim = evidence.claim_id
            if vals.get('public') or vals.get('access_token') or vals.get('company_id',claim.company_id.id)!=claim.company_id.id:
                raise AccessError(self.env._('Evidence files must remain private in the claim company.'))
            return
        claim = self._claim_document(model, record_id, 'write')
        claim._lock_for_edit()
        if claim.state != 'draft':
            raise ValidationError(self.env._('Return the claim to draft before changing its attached documents.'))
        if vals.get('public') or vals.get('access_token'):
            raise AccessError(self.env._('Claim documents cannot be shared publicly or by access token.'))
        if vals.get('company_id') and vals['company_id'] != claim.company_id.id:
            raise ValidationError(self.env._('Attachment company must match its claim.'))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            vals = dict(values)
            defaults = self.default_get(['res_model', 'res_id', 'public', 'access_token'])
            for key in defaults:
                vals.setdefault(key, defaults[key])
            if vals.get('res_model') == 'supplier.shortage.claim.evidence':
                raise AccessError(self.env._('Upload through a new claim evidence record.'))
            self._validate_claim_attachment(vals.get('res_model'), vals.get('res_id'), vals)
            prepared.append(vals)
        return super().create(prepared)

    def _create_evidence_file(self, evidence, values):
        evidence.ensure_one()
        evidence._require_edit()
        if evidence.attachment_id:
            raise AccessError(self.env._('An evidence file cannot be replaced.'))
        values = dict(values, res_model=evidence._name, res_id=evidence.id,
            company_id=evidence.company_id.id, public=False, access_token=False, res_field=False)
        return super(IrAttachment,self.with_context({},allowed_company_ids=self.env.companies.ids)).create(values)

    def write(self, vals):
        self.check_access('write')
        for attachment in self:
            if attachment.res_model == 'supplier.shortage.claim.evidence' or vals.get('res_model') == 'supplier.shortage.claim.evidence':
                raise AccessError(self.env._('Evidence file associations and content are immutable. Upload a new evidence record.'))
            self._validate_claim_attachment(attachment.res_model, attachment.res_id, vals)
            model = vals.get('res_model', attachment.res_model)
            record_id = vals.get('res_id', attachment.res_id)
            self._validate_claim_attachment(model, record_id, vals)
        return super().write(vals)

    def unlink(self):
        if any(a.res_model == 'supplier.shortage.claim.evidence' for a in self):
            raise AccessError(self.env._('Evidence files are retained for audit.'))
        self.check_access('unlink')
        for attachment in self:
            self._validate_claim_attachment(attachment.res_model, attachment.res_id, {})
        return super().unlink()

    def _check_claim_download(self):
        for attachment in self:
            # Token/public streaming may arrive elevated; retain the real request user.
            normal = attachment.sudo(False)
            normal._claim_document(attachment.res_model, attachment.res_id, 'read')

    def _can_return_content(self, field_name=None, access_token=None):
        self._check_claim_download()
        return super()._can_return_content(field_name=field_name, access_token=access_token)

    def _to_http_stream(self):
        self._check_claim_download()
        return super()._to_http_stream()

    def generate_access_token(self):
        self.check_access('read')
        if any(attachment.res_model in CLAIM_MODELS and attachment.res_id for attachment in self):
            raise AccessError(self.env._('Claim documents cannot be shared by access token.'))
        return super().generate_access_token()

    def export_data(self, fields_to_export):
        linked = self.filtered(lambda attachment: attachment.res_model in CLAIM_MODELS and attachment.res_id)
        linked.check_access('read')
        return super().export_data(fields_to_export)
