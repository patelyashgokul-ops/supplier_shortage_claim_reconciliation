{
    'name': 'Supplier Shortage Claim Reconciliation',
    'version': '20.0.13.0.0',
    'category': 'Purchases / Inventory / Supplier Recovery',
    'summary': 'Company-safe supplier recovery claim foundation',
    'description': 'Company-safe purchase claim lines and controlled internal review.',
    'license': 'OPL-1',
    'depends': ['purchase_stock', 'base_report_wkhtmltox'],
    'data': ['security/groups.xml', 'security/ir.access.csv',
             'data/claim_sequence.xml', 'data/submission_report.xml', 'data/submission_email.xml', 'views/shortage_claim_views.xml',
             'views/shortage_claim_menus.xml', 'views/purchase_order_views.xml', 'views/receipt_views.xml', 'views/receiving_exception_views.xml', 'views/billing_views.xml', 'views/detection_views.xml', 'views/claim_preparation_views.xml', 'views/evidence_workspace_views.xml', 'views/claim_approval_views.xml', 'views/claim_submission_views.xml'],
    'application': True,
    'installable': True,
    'auto_install': False,
}



