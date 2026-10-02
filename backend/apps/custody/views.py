"""
收件凭证视图

接口一览（均在 /api/custody/ 下）：
- POST   packets/                 首次收件，创建收件单与 v1 清单
- GET    packets/<no>/            收件单概要（编号为稳定引用）
- GET    packets/<no>/versions/   版本链与各版本时点的完整清单
- POST   packets/<no>/supplement/ 补件（返回每个文件 added/duplicate/name_conflict）
- POST   packets/<no>/revoke/     作废（追加 revocation 版本）
- GET    packets/<no>/verify/     验证 sha256 是否属于指定版本 ?version_no=N
"""
import logging

from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import error_response, success_response

from . import services
from .serializers import (
    PacketCreateSerializer,
    RevokeSerializer,
    digests_from_request,
    to_digest,
)

logger = logging.getLogger('apps')


def _first_error(errors):
    """从 DRF 嵌套错误中取第一条可读信息。"""
    if isinstance(errors, dict):
        value = next(iter(errors.values()))
        return _first_error(value)
    if isinstance(errors, list):
        return _first_error(errors[0]) if errors else '参数错误'
    return str(errors)


class PacketCreateView(APIView):
    """首次收件。"""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        uploads = request.FILES.getlist('file') + request.FILES.getlist('files')
        if uploads:
            digests = [services.digest_upload(upload) for upload in uploads]
            data = {
                key: request.data.get(key)
                for key in ('doc_type', 'title', 'biz_type', 'biz_ref', 'biz_label')
                if request.data.get(key) is not None
            }
            # 大小与哈希均由服务端现场计算，防止 multipart 伪造
            data['files'] = [
                {
                    'file_name': digest.file_name,
                    'file_size': digest.file_size,
                    'sha256': digest.sha256,
                }
                for digest in digests
            ]
        else:
            data = request.data

        serializer = PacketCreateSerializer(data=data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        valid = serializer.validated_data
        result = services.create_packet(
            doc_type=valid['doc_type'],
            title=valid['title'],
            biz_type=valid['biz_type'],
            biz_ref=valid['biz_ref'],
            biz_label=valid.get('biz_label', ''),
            submitter=request.user,
            files=[to_digest(item) for item in valid['files']],
        )
        logger.info(
            'intake packet created %s by %s with %d file(s)',
            result['packet_no'], request.user.username,
            result['counts'][services.RESULT_ADDED],
        )
        return success_response(data=result, message='收件登记成功')


class PacketDetailView(APIView):
    """收件单概要。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, packet_no):
        packet = services.get_packet(packet_no)
        return success_response(data=services.packet_summary(packet))


class PacketVersionListView(APIView):
    """版本链与逐版本清单。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, packet_no):
        packet = services.get_packet(packet_no)
        data = {
            'packet': services.packet_summary(packet),
            'versions': services.list_versions(packet_no),
        }
        return success_response(data=data)


class PacketSupplementView(APIView):
    """补件。"""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, packet_no):
        files, error = digests_from_request(request)
        if error is not None:
            return error_response(message=_first_error(error))

        remark = request.data.get('remark', '')
        remark_serializer = RevokeSerializer(data={'remark': remark})
        if not remark_serializer.is_valid():
            return error_response(message=_first_error(remark_serializer.errors))

        result = services.supplement_packet(
            packet_no=packet_no,
            submitter=request.user,
            files=files,
            remark=remark_serializer.validated_data['remark'],
        )
        if not result['version_created']:
            return success_response(
                data=result,
                message='未发现新内容：文件均为重复或同名冲突，未生成新版本',
            )
        logger.info(
            'intake packet %s supplemented by %s: %s',
            packet_no, request.user.username, result['counts'],
        )
        return success_response(data=result, message='补件已形成新版本')


class PacketRevokeView(APIView):
    """作废。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, packet_no):
        serializer = RevokeSerializer(data=request.data if request.data else {})
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        result = services.revoke_packet(
            packet_no=packet_no,
            submitter=request.user,
            remark=serializer.validated_data['remark'],
        )
        logger.info('intake packet %s revoked by %s', packet_no, request.user.username)
        return success_response(data=result, message='收件单已作废')


class DigestVerifyView(APIView):
    """验证摘要是否属于指定版本。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, packet_no):
        version_no = request.query_params.get('version_no')
        sha256 = request.query_params.get('sha256')
        if not version_no or not sha256:
            return error_response(message='请提供 version_no 与 sha256 查询参数')
        try:
            version_no = int(version_no)
        except (TypeError, ValueError):
            return error_response(message='version_no 必须为正整数')
        if version_no <= 0:
            return error_response(message='version_no 必须为正整数')

        result = services.verify_digest(
            packet_no=packet_no, version_no=version_no, sha256=sha256
        )
        message = '摘要属于该版本' if result['belongs'] else '摘要不属于该版本'
        return success_response(data=result, message=message)
