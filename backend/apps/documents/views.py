"""
文书档案与摘要清单视图
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import error_response, success_response
from . import services
from .models import CustodyDocument, ManifestVersion
from .serializers import (
    CustodyDocumentCreateSerializer,
    CustodyDocumentSerializer,
    CustodyDocumentUpdateSerializer,
    InvalidationSerializer,
    ManifestEntrySerializer,
    ManifestVersionSerializer,
    ReceiptSerializer,
    VerifySerializer,
)

logger = logging.getLogger('apps')


def _first_error(errors):
    """从序列化器错误中取出第一条可读信息（兼容嵌套列表/字典）。"""
    first = list(errors.values())[0]
    while isinstance(first, (list, dict)):
        first = list(first.values())[0] if isinstance(first, dict) else first[0]
    return str(first)


def _get_document(doc_no):
    try:
        return CustodyDocument.objects.get(doc_no=doc_no)
    except CustodyDocument.DoesNotExist:
        return None


class DocumentListView(APIView):
    """业务记录列表与创建"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = CustodyDocument.objects.all().order_by('-created_at')

        doc_type = request.query_params.get('doc_type')
        if doc_type:
            queryset = queryset.filter(doc_type=doc_type)
        keyword = request.query_params.get('keyword')
        if keyword:
            queryset = queryset.filter(doc_no__icontains=keyword) | queryset.filter(title__icontains=keyword)

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        serializer = CustodyDocumentSerializer(queryset[start:end], many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })

    def post(self, request):
        """创建业务记录，业务编号创建后不可变更"""
        serializer = CustodyDocumentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        document = CustodyDocument.objects.create(
            doc_no=serializer.validated_data['doc_no'],
            doc_type=serializer.validated_data['doc_type'],
            title=serializer.validated_data['title'],
            remark=serializer.validated_data['remark'],
            created_by=request.user,
        )

        logger.info(f"User {request.user.username} created document {document.doc_no}")

        return success_response(data=CustodyDocumentSerializer(document).data, message='创建成功')


class DocumentDetailView(APIView):
    """业务记录详情与更新"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        data = CustodyDocumentSerializer(document).data
        last_version = document.versions.order_by('-version_no').first()
        data['current_version_no'] = last_version.version_no if last_version else 0
        data['active_file_count'] = services.get_active_entries(document).count()
        return success_response(data=data)

    def put(self, request, doc_no):
        """更新标题、备注；业务编号不可变更，清单引用保持稳定"""
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        if 'doc_no' in request.data and request.data['doc_no'] != document.doc_no:
            return error_response(message='业务编号一经创建不可变更')

        serializer = CustodyDocumentUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        document.title = serializer.validated_data['title']
        document.remark = serializer.validated_data['remark']
        document.save()

        logger.info(f"User {request.user.username} updated document {document.doc_no}")

        return success_response(data=CustodyDocumentSerializer(document).data, message='更新成功')


class ReceiptView(APIView):
    """收件登记：为业务记录追加新的清单版本（补件同为新版本，不覆盖旧摘要）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        serializer = ReceiptSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        result = services.receive_files(
            document_id=document.id,
            files=serializer.validated_data['files'],
            note=serializer.validated_data['note'],
            user=request.user,
        )

        data = {
            'result': result['result'],
            'version': ManifestVersionSerializer(result['version']).data if result['version'] else None,
            'registered_count': len(result['entries']),
            'duplicates': result['duplicates'],
            'name_conflicts': result['name_conflicts'],
        }

        if result['result'] == 'duplicate':
            logger.info(
                f"User {request.user.username} receipt on {doc_no} fully duplicated, no version created"
            )
            return success_response(data=data, message='文件均已登记过，未产生新版本')

        logger.info(
            f"User {request.user.username} created manifest version "
            f"{doc_no} v{result['version'].version_no} ({result['version'].reason})"
        )
        return success_response(data=data, message='收件成功')


class InvalidationView(APIView):
    """作废登记：以新版本作废既有条目，原摘要保持原样"""
    permission_classes = [IsAuthenticated]

    def post(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        serializer = InvalidationSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        result = services.invalidate_entries(
            document_id=document.id,
            entry_ids=serializer.validated_data['entry_ids'],
            note=serializer.validated_data['note'],
            user=request.user,
        )

        data = {
            'result': result['result'],
            'version': ManifestVersionSerializer(result['version']).data if result['version'] else None,
            'voided_entry_ids': result['voided_entry_ids'],
            'already_voided_entry_ids': result['already_voided_entry_ids'],
        }

        if result['result'] == 'already_voided':
            return success_response(data=data, message='目标条目此前均已作废，未产生新版本')

        logger.info(
            f"User {request.user.username} voided entries {result['voided_entry_ids']} "
            f"on {doc_no} in v{result['version'].version_no}"
        )
        return success_response(data=data, message='作废成功')


class VersionListView(APIView):
    """清单版本列表"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        queryset = ManifestVersion.objects.filter(document=document).order_by('-version_no')

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        serializer = ManifestVersionSerializer(queryset[start:end], many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })


class VersionDetailView(APIView):
    """清单版本详情（含全部条目与清单哈希）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_no, version_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        version = ManifestVersion.objects.filter(document=document, version_no=version_no).first()
        if version is None:
            return error_response(message='清单版本不存在', code=404)

        return success_response(data=ManifestVersionSerializer(version).data)


class VersionVerifyView(APIView):
    """验证某个摘要是否属于指定版本"""
    permission_classes = [IsAuthenticated]

    def post(self, request, doc_no, version_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        serializer = VerifySerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        result = services.verify_digest_in_version(
            document=document,
            version_no=version_no,
            sha256=serializer.validated_data['sha256'],
            file_name=serializer.validated_data.get('file_name'),
            file_size=serializer.validated_data.get('file_size'),
        )

        return success_response(data={
            'matched': result['matched'],
            'doc_no': document.doc_no,
            'version_no': version_no,
            'entries': ManifestEntrySerializer(result['entries'], many=True).data,
            'mismatches': result['mismatches'],
        }, message='验证完成')


class CurrentFilesView(APIView):
    """当前有效文件清单（已登记且未作废）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        entries = services.get_active_entries(document)
        return success_response(data={
            'doc_no': document.doc_no,
            'total': entries.count(),
            'list': ManifestEntrySerializer(entries, many=True).data,
        })


class DocumentVerifyView(APIView):
    """验证某个摘要当前是否有效（曾登记但已作废的会明确标出）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, doc_no):
        document = _get_document(doc_no)
        if document is None:
            return error_response(message='业务记录不存在', code=404)

        serializer = VerifySerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        result = services.verify_digest_current(
            document=document,
            sha256=serializer.validated_data['sha256'],
        )

        return success_response(data={
            'matched': result['matched'],
            'status': result['status'],
            'doc_no': document.doc_no,
            'entry': ManifestEntrySerializer(result['entry']).data if result['entry'] else None,
            'voided_in_version': result['voided_in_version'],
        }, message='验证完成')
