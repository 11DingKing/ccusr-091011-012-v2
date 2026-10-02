"""
收件凭证 URL 配置
"""
from django.urls import path

from .views import (
    DigestVerifyView,
    PacketCreateView,
    PacketDetailView,
    PacketRevokeView,
    PacketSupplementView,
    PacketVersionListView,
)

urlpatterns = [
    path('custody/packets/', PacketCreateView.as_view(), name='custody-packet-create'),
    path('custody/packets/<str:packet_no>/', PacketDetailView.as_view(), name='custody-packet-detail'),
    path('custody/packets/<str:packet_no>/versions/', PacketVersionListView.as_view(), name='custody-packet-versions'),
    path('custody/packets/<str:packet_no>/supplement/', PacketSupplementView.as_view(), name='custody-packet-supplement'),
    path('custody/packets/<str:packet_no>/revoke/', PacketRevokeView.as_view(), name='custody-packet-revoke'),
    path('custody/packets/<str:packet_no>/verify/', DigestVerifyView.as_view(), name='custody-packet-verify'),
]
