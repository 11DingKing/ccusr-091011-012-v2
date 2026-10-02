"""
收件凭证序列化器
"""
import re

from rest_framework import serializers

from .models import DOC_TYPE_CHOICES
from .services import FileDigest, digest_upload

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


class FileDigestSerializer(serializers.Serializer):
    """单文件摘要输入。"""

    file_name = serializers.CharField(max_length=255)
    file_size = serializers.IntegerField(min_value=0)
    sha256 = serializers.CharField(min_length=64, max_length=64)

    def validate_file_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('文件名不能为空')
        return value

    def validate_sha256(self, value):
        value = value.strip().lower()
        if not _SHA256_RE.match(value):
            raise serializers.ValidationError('SHA-256 必须为 64 位十六进制字符串')
        return value


def to_digest(value):
    """将校验后的文件字典（嵌套序列化器产物）转为 FileDigest。"""
    return FileDigest(
        file_name=value['file_name'],
        file_size=value['file_size'],
        sha256=value['sha256'],
    )


class PacketCreateSerializer(serializers.Serializer):
    """首次收件。files 支持 JSON 摘要数组；multipart 上传时由视图直接注入。"""

    doc_type = serializers.ChoiceField(choices=DOC_TYPE_CHOICES)
    title = serializers.CharField(min_length=1, max_length=200)
    biz_type = serializers.CharField(min_length=1, max_length=50)
    biz_ref = serializers.CharField(min_length=1, max_length=100)
    biz_label = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    files = FileDigestSerializer(many=True)

    def validate_files(self, value):
        if not value:
            raise serializers.ValidationError('首次收件至少需要一个文件摘要')
        return value

    def validate(self, attrs):
        files = attrs['files']
        sha_seen = set()
        name_seen = set()
        for item in files:
            digest = to_digest(item)
            if digest.sha256 in sha_seen:
                raise serializers.ValidationError({
                    'files': f'批次内存在重复文件内容：{digest.file_name}'
                })
            if digest.file_name in name_seen:
                raise serializers.ValidationError({
                    'files': f'批次内存在同名文件：{digest.file_name}'
                })
            sha_seen.add(digest.sha256)
            name_seen.add(digest.file_name)
        return attrs


class RevokeSerializer(serializers.Serializer):
    """作废 / 补件备注。"""

    remark = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')


def digests_from_request(request):
    """从请求提取文件摘要：multipart 文件现场算哈希，否则读取 JSON files。

    multipart 时字段名可为 file / files，支持多文件。
    """
    uploads = request.FILES.getlist('file') + request.FILES.getlist('files')
    if uploads:
        return [digest_upload(upload) for upload in uploads], None

    raw_files = request.data.get('files')
    if not isinstance(raw_files, list):
        return None, 'files 必须为文件摘要数组（或直接上传文件）'

    serializer = FileDigestSerializer(data=raw_files, many=True)
    if not serializer.is_valid():
        return None, serializer.errors
    return [to_digest(item) for item in serializer.validated_data], None
