"""
文书档案与摘要清单URL配置
"""
from django.urls import path

from .views import (
    CurrentFilesView,
    DocumentDetailView,
    DocumentListView,
    DocumentVerifyView,
    InvalidationView,
    ReceiptView,
    VersionDetailView,
    VersionListView,
    VersionVerifyView,
)

urlpatterns = [
    # 业务记录
    path('documents/', DocumentListView.as_view(), name='document-list'),
    path('documents/<str:doc_no>/', DocumentDetailView.as_view(), name='document-detail'),

    # 收件与作废（均追加新清单版本）
    path('documents/<str:doc_no>/receipts/', ReceiptView.as_view(), name='document-receipt'),
    path('documents/<str:doc_no>/invalidations/', InvalidationView.as_view(), name='document-invalidation'),

    # 清单版本
    path('documents/<str:doc_no>/versions/', VersionListView.as_view(), name='manifest-version-list'),
    path('documents/<str:doc_no>/versions/<int:version_no>/', VersionDetailView.as_view(), name='manifest-version-detail'),
    path('documents/<str:doc_no>/versions/<int:version_no>/verify/', VersionVerifyView.as_view(), name='manifest-version-verify'),

    # 当前有效清单与摘要验证
    path('documents/<str:doc_no>/files/', CurrentFilesView.as_view(), name='document-current-files'),
    path('documents/<str:doc_no>/verify/', DocumentVerifyView.as_view(), name='document-verify'),
]
