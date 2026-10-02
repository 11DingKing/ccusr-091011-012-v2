"""
文书档案与摘要清单序列化器
"""
import re

from rest_framework import serializers

from .models import CustodyDocument, ManifestEntry, ManifestVersion

DOC_NO_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$')
SHA256_PATTERN = re.compile(r'^[0-9a-fA-F]{64}$')


class CustodyDocumentSerializer(serializers.ModelSerializer):
    """业务记录序列化器"""
    doc_type_display = serializers.CharField(source='get_doc_type_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = CustodyDocument
        fields = [
            'id', 'doc_no', 'doc_type', 'doc_type_display', 'title', 'remark',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'doc_no', 'created_at', 'updated_at']


class CustodyDocumentCreateSerializer(serializers.Serializer):
    """业务记录创建序列化器"""
    doc_no = serializers.CharField(required=True, error_messages={
        'required': '请输入业务编号',
        'blank': '业务编号不能为空',
    })
    doc_type = serializers.ChoiceField(
        choices=[c[0] for c in CustodyDocument.DOC_TYPE_CHOICES],
        required=True,
        error_messages={'required': '请选择文书类型', 'invalid_choice': '文书类型无效'},
    )
    title = serializers.CharField(min_length=1, max_length=100, required=True, error_messages={
        'required': '请输入标题',
        'blank': '标题不能为空',
        'max_length': '标题最多100个字',
    })
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_doc_no(self, value):
        value = value.strip()
        if not DOC_NO_PATTERN.match(value):
            raise serializers.ValidationError('业务编号须为1-32位字母、数字、短横线或下划线，且以字母或数字开头')
        if CustodyDocument.objects.filter(doc_no=value).exists():
            raise serializers.ValidationError('业务编号已存在')
        return value


class CustodyDocumentUpdateSerializer(serializers.Serializer):
    """业务记录更新序列化器（业务编号不可变更）"""
    title = serializers.CharField(min_length=1, max_length=100, required=True, error_messages={
        'required': '请输入标题',
        'blank': '标题不能为空',
        'max_length': '标题最多100个字',
    })
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class FileDigestSerializer(serializers.Serializer):
    """单个文件摘要序列化器"""
    file_name = serializers.CharField(required=True, max_length=255, error_messages={
        'required': '请输入文件名',
        'blank': '文件名不能为空',
        'max_length': '文件名最多255个字符',
    })
    file_size = serializers.IntegerField(min_value=0, required=True, error_messages={
        'required': '请输入文件大小',
        'min_value': '文件大小不能为负数',
        'invalid': '文件大小须为整数（字节）',
    })
    sha256 = serializers.CharField(required=True, error_messages={
        'required': '请输入文件SHA-256摘要',
        'blank': '文件SHA-256摘要不能为空',
    })

    def validate_file_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('文件名不能为空')
        return value

    def validate_sha256(self, value):
        value = value.strip().lower()
        if not SHA256_PATTERN.match(value):
            raise serializers.ValidationError('SHA-256摘要须为64位十六进制字符')
        return value


class ReceiptSerializer(serializers.Serializer):
    """收件请求序列化器"""
    files = serializers.ListField(
        child=FileDigestSerializer(), min_length=1, max_length=100,
        required=True, error_messages={
            'required': '请提供文件摘要列表',
            'empty': '文件摘要列表不能为空',
            'min_length': '至少提供一个文件摘要',
            'max_length': '单次收件最多100个文件',
        },
    )
    note = serializers.CharField(required=False, allow_blank=True, default='', max_length=500)


class InvalidationSerializer(serializers.Serializer):
    """作废请求序列化器"""
    entry_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1,
        required=True, error_messages={
            'required': '请提供要作废的条目ID列表',
            'empty': '条目ID列表不能为空',
            'min_length': '至少提供一个条目ID',
        },
    )
    note = serializers.CharField(required=False, allow_blank=True, default='', max_length=500)


class VerifySerializer(serializers.Serializer):
    """摘要验证请求序列化器"""
    sha256 = serializers.CharField(required=True, error_messages={
        'required': '请输入要验证的SHA-256摘要',
        'blank': 'SHA-256摘要不能为空',
    })
    file_name = serializers.CharField(required=False, allow_blank=True, default=None)
    file_size = serializers.IntegerField(required=False, min_value=0, default=None)

    def validate_sha256(self, value):
        value = value.strip().lower()
        if not SHA256_PATTERN.match(value):
            raise serializers.ValidationError('SHA-256摘要须为64位十六进制字符')
        return value


class ManifestEntrySerializer(serializers.ModelSerializer):
    """清单条目序列化器"""
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = ManifestEntry
        fields = [
            'id', 'action', 'action_display', 'file_name', 'file_size',
            'sha256', 'voids', 'created_at'
        ]


class ManifestVersionSerializer(serializers.ModelSerializer):
    """清单版本序列化器（含条目与哈希链）"""
    entries = ManifestEntrySerializer(many=True, read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)

    class Meta:
        model = ManifestVersion
        fields = [
            'id', 'version_no', 'reason', 'reason_display', 'note',
            'doc_no_snapshot', 'doc_title_snapshot',
            'submitted_by', 'submitted_by_name',
            'prev_hash', 'manifest_hash', 'entries', 'created_at'
        ]
