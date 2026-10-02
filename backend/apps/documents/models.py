"""
文书档案与不可变摘要清单模型。

保管服务接收鉴定报告、移交文书的文件摘要（文件名、大小、哈希、提交人），
每次收件生成一个不可变的清单版本；后续补件与作废均通过追加新版本表达，
历史版本及其摘要一旦写入即不可修改、不可删除。
"""
from django.db import models

from apps.authentication.models import User


class ImmutableRecordError(Exception):
    """不可变记录被修改或删除时抛出。"""


class CustodyDocument(models.Model):
    """业务记录（鉴定报告 / 移交文书）。

    doc_no 为稳定的业务编号，创建后不可变更；清单版本通过该编号关联业务，
    业务记录的标题、备注等信息变更不影响清单引用。
    """

    DOC_TYPE_CHOICES = [
        ('appraisal', '鉴定报告'),
        ('transfer', '移交文书'),
    ]

    doc_no = models.CharField('业务编号', max_length=32, unique=True)
    doc_type = models.CharField('文书类型', max_length=20, choices=DOC_TYPE_CHOICES)
    title = models.CharField('标题', max_length=100)
    remark = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_documents', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'doc_document'
        verbose_name = '业务记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.doc_no} - {self.title}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = CustodyDocument.objects.get(pk=self.pk)
            if original.doc_no != self.doc_no:
                raise ImmutableRecordError('业务编号一经创建不可变更')
        super().save(*args, **kwargs)


class ManifestVersion(models.Model):
    """清单版本：一次收件（或一次作废）的不可变记录。

    版本上快照了当时的业务编号、业务标题与提交人，并携带
    prev_hash / manifest_hash 哈希链，可证明历史清单未被篡改。
    """

    REASON_CHOICES = [
        ('initial', '首次收件'),
        ('supplement', '补件'),
        ('invalidation', '作废'),
    ]

    document = models.ForeignKey(
        CustodyDocument, on_delete=models.PROTECT,
        related_name='versions', verbose_name='业务记录'
    )
    version_no = models.PositiveIntegerField('版本号')
    reason = models.CharField('版本原因', max_length=20, choices=REASON_CHOICES)
    note = models.TextField('备注', blank=True)
    doc_no_snapshot = models.CharField('业务编号快照', max_length=32)
    doc_title_snapshot = models.CharField('业务标题快照', max_length=100)
    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='submitted_manifests', verbose_name='提交人'
    )
    submitted_by_name = models.CharField('提交人快照', max_length=50)
    prev_hash = models.CharField('上一版本清单哈希', max_length=64, blank=True)
    manifest_hash = models.CharField('清单哈希', max_length=64)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'doc_manifest_version'
        verbose_name = '清单版本'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['document', 'version_no']

    def __str__(self):
        return f"{self.doc_no_snapshot} v{self.version_no}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError('清单版本不可修改')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError('清单版本不可删除')


class ManifestEntry(models.Model):
    """清单条目：单个文件摘要，或一条作废记录。

    action='add'  登记文件摘要（文件名、大小、哈希）；
    action='void' 作废此前某条登记，voids 指向被作废条目，
                  并快照其文件名、大小、哈希，原条目保持原样不覆盖。
    """

    ACTION_CHOICES = [
        ('add', '登记'),
        ('void', '作废'),
    ]

    version = models.ForeignKey(
        ManifestVersion, on_delete=models.CASCADE,
        related_name='entries', verbose_name='清单版本'
    )
    action = models.CharField('动作', max_length=10, choices=ACTION_CHOICES, default='add')
    file_name = models.CharField('文件名', max_length=255)
    file_size = models.BigIntegerField('文件大小（字节）')
    sha256 = models.CharField('SHA-256摘要', max_length=64)
    voids = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='voided_by', verbose_name='作废目标'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'doc_manifest_entry'
        verbose_name = '清单条目'
        verbose_name_plural = verbose_name
        ordering = ['id']
        indexes = [models.Index(fields=['sha256'])]

    def __str__(self):
        return f"{self.get_action_display()} {self.file_name} ({self.sha256[:12]}…)"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError('清单条目不可修改')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError('清单条目不可删除')
