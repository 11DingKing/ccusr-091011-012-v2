"""
收件凭证（不可变清单 / 版本链）测试
"""
import hashlib
import tempfile
import threading
from copy import deepcopy
from datetime import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.core.exceptions import BusinessException, NotFoundException

from .models import (
    CHANGE_SUPPLEMENT,
    FrozenModelError,
    IntakeAttachment,
    IntakePacket,
    IntakeVersion,
    VERSION_ACTIVE,
    VERSION_REVOKED,
    VERSION_SUPERSEDED,
)
from . import services
from .services import FileDigest


def sha(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def digest(name, content, size=None):
    raw = content.encode('utf-8')
    return FileDigest(name, len(raw) if size is None else size, hashlib.sha256(raw).hexdigest())


class CustodyFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'custody-clerk', 'testpass123', role='admin', real_name='王保管'
        )
        self.other = User.objects.create_user(
            'custody-clerk-2', 'testpass123', role='user', real_name='李复核'
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(self.user)}')

        self.f_report = digest('鉴定报告.pdf', '鉴定报告正文-初版')
        self.f_handover = digest('移交文书.pdf', '移交文书正文')
        self.packet = self._create([self.f_report, self.f_handover])

    def _create(self, files, **kwargs):
        return services.create_packet(
            doc_type=kwargs.get('doc_type', 'appraisal_report'),
            title=kwargs.get('title', '三季度鉴定件'),
            biz_type=kwargs.get('biz_type', 'case'),
            biz_ref=kwargs.get('biz_ref', 'CASE-2026-009'),
            biz_label=kwargs.get('biz_label', '某案扣押材料'),
            submitter=self.user,
            files=files,
        )

    @property
    def packet_no(self):
        return self.packet['packet_no']

    def assert_rejected(self, write_call, *extra_exceptions):
        """断言写操作被不可变机制拒绝。

        Model/QuerySet 的 delete 与部分 save 运行在内部 atomic 中，
        拒绝会污染当前事务；用保存点隔离，保证后续断言仍可查询。
        PROTECT 外键（删除收件锚点时）也属于有效防线，允许一并断言。
        """
        from django.db.models.deletion import ProtectedError
        expected = (FrozenModelError, ProtectedError, *extra_exceptions)
        with transaction.atomic():
            with self.assertRaises(expected):
                write_call()


class ImmutableManifestModelTest(CustodyFixture):
    def test_attachment_rows_capture_size_hash_submitter_biz(self):
        rows = IntakeAttachment.objects.filter(packet__packet_no=self.packet_no).order_by('id')
        self.assertEqual(rows.count(), 2)
        row = rows[0]
        self.assertEqual(row.file_size, len('鉴定报告正文-初版'.encode('utf-8')))
        self.assertEqual(row.sha256, self.f_report.sha256)
        self.assertEqual(row.submitted_by_id, self.user.id)
        self.assertEqual(row.submitted_by_name, '王保管')
        self.assertEqual(row.biz_type, 'case')
        self.assertEqual(row.biz_ref, 'CASE-2026-009')

    def test_attachment_rows_cannot_be_updated(self):
        row = IntakeAttachment.objects.first()

        def tamper_instance():
            row.file_name = '被替换的文件名.pdf'
            row.save()

        self.assert_rejected(tamper_instance)
        row.refresh_from_db()
        self.assertEqual(row.file_name, '鉴定报告.pdf')

        # queryset 级别的更新同样被拦截
        self.assert_rejected(
            lambda: IntakeAttachment.objects.filter(pk=row.pk).update(file_name='x.pdf')
        )

    def test_attachment_rows_cannot_be_deleted(self):
        row = IntakeAttachment.objects.first()
        self.assert_rejected(row.delete)
        self.assert_rejected(
            lambda: IntakeAttachment.objects.filter(pk=row.pk).delete()
        )
        self.assertEqual(IntakeAttachment.objects.count(), 2)

    def test_packet_anchor_is_frozen(self):
        packet = IntakePacket.objects.get(packet_no=self.packet_no)

        def tamper_packet():
            packet.biz_ref = 'CASE-CHANGED'
            packet.save()

        self.assert_rejected(tamper_packet)
        self.assert_rejected(packet.delete)

    def test_version_content_frozen_but_status_can_transition(self):
        version = IntakeVersion.objects.get(version_no=1)

        def tamper_remark():
            version.remark = '篡改备注'
            version.save()

        self.assert_rejected(tamper_remark)
        # 重新加载：上面被拒绝的修改仅残留在实例内存中
        version.refresh_from_db()

        # 合法状态迁移
        version.status = VERSION_SUPERSEDED
        version.save(update_fields=['status'])
        version.refresh_from_db()
        self.assertEqual(version.status, VERSION_SUPERSEDED)

        # superseded -> active 属于逆向迁移，拒绝
        version.status = VERSION_ACTIVE
        self.assert_rejected(lambda: version.save(update_fields=['status']))
        self.assert_rejected(version.delete)

    def test_submitter_snapshot_survives_user_rename(self):
        """账号改名不影响既有清单行的提交人快照与外键引用。"""
        row = IntakeAttachment.objects.first()
        self.user.real_name = '王保管（已调岗）'
        self.user.username = 'renamed-clerk'
        self.user.save()
        row.refresh_from_db()
        self.assertEqual(row.submitted_by_name, '王保管')
        self.assertEqual(row.submitted_by_id, self.user.id)


class VersionChainServiceTest(CustodyFixture):
    def test_duplicate_upload_creates_no_version(self):
        result = services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user,
            files=[digest('鉴定报告.pdf', '鉴定报告正文-初版')],
        )
        self.assertFalse(result['version_created'])
        self.assertIsNone(result['version_no'])
        self.assertEqual(result['counts'], {'added': 0, 'duplicate': 1, 'name_conflict': 0})
        self.assertEqual(IntakeVersion.objects.count(), 1)

    def test_same_bytes_different_name_is_duplicate(self):
        result = services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user,
            files=[digest('另一个名字.pdf', '鉴定报告正文-初版')],
        )
        self.assertFalse(result['version_created'])
        item = result['files'][0]
        self.assertEqual(item['result'], 'duplicate')
        # 带回首次登记时的文件名，便于定位
        self.assertEqual(item['registered_name'], '鉴定报告.pdf')
        self.assertEqual(item['registered_version_no'], 1)

    def test_same_name_different_content_is_conflict(self):
        result = services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user,
            files=[digest('鉴定报告.pdf', '完全不同的鉴定报告内容')],
        )
        self.assertFalse(result['version_created'])
        item = result['files'][0]
        self.assertEqual(item['result'], 'name_conflict')
        self.assertEqual(item['existing_version_no'], 1)
        self.assertEqual(item['existing_sha256'], self.f_report.sha256)
        # 旧行未被覆盖
        row = IntakeAttachment.objects.get(sha256=self.f_report.sha256)
        self.assertEqual(row.file_name, '鉴定报告.pdf')

    def test_supplement_appends_version_and_supersedes_old(self):
        v1_before = IntakeVersion.objects.get(version_no=1)
        self.assertEqual(v1_before.status, VERSION_ACTIVE)

        addon = digest('补充说明.pdf', '后补的情况说明')
        result = services.supplement_packet(
            packet_no=self.packet_no, submitter=self.other,
            files=[addon, digest('鉴定报告.pdf', '鉴定报告正文-初版')],
            remark='复查后补件',
        )
        self.assertTrue(result['version_created'])
        self.assertEqual(result['version_no'], 2)
        self.assertEqual(result['counts'], {'added': 1, 'duplicate': 1, 'name_conflict': 0})

        v1 = IntakeVersion.objects.get(version_no=1)
        v2 = IntakeVersion.objects.get(version_no=2)
        self.assertEqual(v1.status, VERSION_SUPERSEDED)
        self.assertEqual(v2.status, VERSION_ACTIVE)
        self.assertEqual(v2.change_type, CHANGE_SUPPLEMENT)
        self.assertEqual(v2.submitted_by_id, self.other.id)

        # v1 的两行内容原样保留，未被覆盖
        self.assertEqual(
            set(IntakeAttachment.objects.filter(version=v1).values_list('sha256', flat=True)),
            {self.f_report.sha256, self.f_handover.sha256},
        )
        # v2 时点的整单清单 = 旧两行 + 新一行
        self.assertEqual(IntakeAttachment.objects.filter(version=v2).count(), 1)

    def test_revoke_creates_revocation_version_and_blocks_further_changes(self):
        result = services.revoke_packet(
            packet_no=self.packet_no, submitter=self.user, remark='材料撤回'
        )
        self.assertEqual(result['version_no'], 2)
        self.assertEqual(result['status'], VERSION_REVOKED)
        self.assertEqual(result['prior_status'], VERSION_ACTIVE)

        v1 = IntakeVersion.objects.get(version_no=1)
        v2 = IntakeVersion.objects.get(version_no=2)
        self.assertEqual(v1.status, VERSION_REVOKED)
        self.assertEqual(v2.change_type, 'revocation')
        # 作废版本不含新清单行，旧清单全部保留
        self.assertEqual(IntakeAttachment.objects.count(), 2)

        with self.assertRaises(BusinessException) as ctx:
            services.revoke_packet(packet_no=self.packet_no, submitter=self.user)
        self.assertEqual(ctx.exception.code, 409)
        with self.assertRaises(BusinessException):
            services.supplement_packet(
                packet_no=self.packet_no, submitter=self.user,
                files=[digest('x.pdf', 'x')],
            )

    def test_manifest_hash_is_stable_and_recomputable(self):
        addon = digest('补充说明.pdf', '后补的情况说明')
        r1 = self.packet
        r2 = services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user, files=[addon]
        )
        # 每次复核重算结果与落库指纹一致
        self.assertTrue(
            services.verify_digest(
                packet_no=self.packet_no, version_no=1, sha256=self.f_report.sha256
            )['manifest_match']
        )
        self.assertTrue(
            services.verify_digest(
                packet_no=self.packet_no, version_no=2, sha256=addon.sha256
            )['manifest_match']
        )
        self.assertNotEqual(r1['manifest_hash'], r2['manifest_hash'])

    def test_verify_digest_membership_and_current_status(self):
        addon = digest('补充说明.pdf', '后补的情况说明')
        services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user, files=[addon]
        )
        hit = services.verify_digest(
            packet_no=self.packet_no, version_no=1, sha256=self.f_report.sha256
        )
        self.assertTrue(hit['belongs'])
        self.assertTrue(hit['in_current_version'])
        self.assertEqual(hit['current_version_no'], 2)

        # 补件文件不属于 v1，但能查到首次登记版本
        miss = services.verify_digest(
            packet_no=self.packet_no, version_no=1, sha256=addon.sha256
        )
        self.assertFalse(miss['belongs'])
        self.assertEqual(miss['first_registered_version_no'], 2)

        # 完全无关的哈希
        unknown = services.verify_digest(
            packet_no=self.packet_no, version_no=1, sha256=sha('不存在')
        )
        self.assertFalse(unknown['belongs'])
        self.assertIsNone(unknown['file_name'])

        with self.assertRaises(NotFoundException):
            services.verify_digest(
                packet_no=self.packet_no, version_no=99, sha256=addon.sha256
            )


class CustodyAPITest(CustodyFixture):
    def test_create_packet_via_json(self):
        body = {
            'doc_type': 'handover_document',
            'title': '移交一批',
            'biz_type': 'transfer',
            'biz_ref': 'TR-77',
            'files': [
                {'file_name': 'a.pdf', 'file_size': 3, 'sha256': sha('abc')},
            ],
        }
        response = self.client.post('/api/custody/packets/', body, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(data['version_no'], 1)
        self.assertEqual(data['files'][0]['result'], 'added')
        self.assertTrue(data['packet_no'].startswith('NB'))

    def test_create_packet_via_multipart_hashes_server_side(self):
        payload = SimpleUploadedFile('证据照片.png', b'binary-image-bytes')
        response = self.client.post(
            '/api/custody/packets/',
            {
                'doc_type': 'appraisal_report',
                'title': '上传现场算哈希',
                'biz_type': 'case',
                'biz_ref': 'CASE-1',
                'file': payload,
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        row = IntakeAttachment.objects.get(packet__packet_no=data['packet_no'])
        self.assertEqual(row.sha256, hashlib.sha256(b'binary-image-bytes').hexdigest())
        self.assertEqual(row.file_size, len(b'binary-image-bytes'))

    def test_reject_bad_sha256(self):
        body = {
            'doc_type': 'appraisal_report',
            'title': 't', 'biz_type': 'case', 'biz_ref': 'c1',
            'files': [{'file_name': 'a.pdf', 'file_size': 1, 'sha256': 'abc'}],
        }
        response = self.client.post('/api/custody/packets/', body, format='json')
        self.assertEqual(response.status_code, 400)

    def test_supplement_api_reports_three_outcomes(self):
        addon = digest('新材料.pdf', '新内容')
        body = {'files': [
            # 重复
            {'file_name': '鉴定报告.pdf', 'file_size': self.f_report.file_size,
             'sha256': self.f_report.sha256},
            # 同名不同内容
            {'file_name': '移交文书.pdf', 'file_size': 9, 'sha256': sha('调包内容')},
            # 新增
            {'file_name': addon.file_name, 'file_size': addon.file_size,
             'sha256': addon.sha256},
        ]}
        response = self.client.post(
            f'/api/custody/packets/{self.packet_no}/supplement/', body, format='json'
        )
        self.assertEqual(response.status_code, 200, response.content)
        results = {item['file_name']: item['result'] for item in response.json()['data']['files']}
        self.assertEqual(results['鉴定报告.pdf'], 'duplicate')
        self.assertEqual(results['移交文书.pdf'], 'name_conflict')
        self.assertEqual(results['新材料.pdf'], 'added')
        # 只有新增产生了新版本
        self.assertEqual(response.json()['data']['version_no'], 2)

    def test_version_list_and_verify_endpoint(self):
        addon = digest('补充说明.pdf', '后补的情况说明')
        services.supplement_packet(
            packet_no=self.packet_no, submitter=self.user, files=[addon]
        )
        versions = self.client.get(f'/api/custody/packets/{self.packet_no}/versions/')
        self.assertEqual(versions.status_code, 200)
        payload = versions.json()['data']
        self.assertEqual([v['version_no'] for v in payload['versions']], [1, 2])
        # v1 时点的清单切片只有 2 行，v2 时点有 3 行
        self.assertEqual(len(payload['versions'][0]['attachments']), 2)
        self.assertEqual(len(payload['versions'][1]['attachments']), 3)

        ok = self.client.get(
            f'/api/custody/packets/{self.packet_no}/verify/',
            {'version_no': 1, 'sha256': self.f_report.sha256},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()['data']['belongs'])

        bad = self.client.get(
            f'/api/custody/packets/{self.packet_no}/verify/',
            {'version_no': 1, 'sha256': addon.sha256},
        )
        self.assertFalse(bad.json()['data']['belongs'])

    def test_revoke_api(self):
        response = self.client.post(
            f'/api/custody/packets/{self.packet_no}/revoke/',
            {'remark': '作废原因'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['status'], VERSION_REVOKED)

    def test_packet_reference_remains_stable(self):
        detail = self.client.get(f'/api/custody/packets/{self.packet_no}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['data']['biz_ref'], 'CASE-2026-009')
        self.assertEqual(detail.json()['data']['current_version_no'], 1)

    def test_requires_authentication(self):
        response = APIClient().get(f'/api/custody/packets/{self.packet_no}/')
        self.assertEqual(response.status_code, 401)


class ConcurrentSupplementTest(TransactionTestCase):
    """并发补件：相同文件两线程同时提交，必须一个 added、一个 duplicate。

    使用独立文件库并跳过 Django 的 flush（append-only 触发器会拦截
    flush 发出的 DELETE）；用例结束后删除文件库并切回内存测试库。
    """

    def _reset_connection_settings(self):
        """令 ConnectionHandler 按当前 settings.DATABASES 惰性重建连接配置。

        除了清空全局配置缓存，还必须删除线程局部缓存的 DatabaseWrapper：
        它在创建时就绑定了旧 settings_dict，close() 不会更新它。
        """
        connections.close_all()
        connections._settings = None
        connections.__dict__.pop('settings', None)
        for alias in connections:
            try:
                delattr(connections._connections, alias)
            except AttributeError:
                pass

    def _fixture_setup(self):
        # 跳过默认 flush：本用例切换到独立的一次性文件库
        pass

    def _fixture_teardown(self):
        # 清理已在 tearDown 内完成；此处不能 close_all，否则会销毁内存库
        pass

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

        from django.conf import settings as django_settings
        test_databases = deepcopy(django_settings.DATABASES)
        test_databases['default']['NAME'] = self.db_path
        test_databases['default'].pop('TEST', None)

        self._context = override_settings(DATABASES=test_databases)
        self._context.enable()
        self._reset_connection_settings()
        call_command('migrate', verbosity=0)

        self.user = User.objects.create_user('concurrent-clerk', 'pw', role='admin')
        created = services.create_packet(
            doc_type='appraisal_report', title='并发用例',
            biz_type='case', biz_ref='CASE-CONC', submitter=self.user,
            files=[digest('既有文件.pdf', '初始内容')],
        )
        self.packet_no = created['packet_no']

    def tearDown(self):
        # 先在文件库配置上关闭连接，再解除覆盖并切回内存测试库
        self._reset_connection_settings()
        self._context.disable()
        self._reset_connection_settings()
        # 重建内存库 schema（连接全部关闭后内存库已销毁），并保持连接存活
        call_command('migrate', verbosity=0)
        import os
        os.unlink(self.db_path)

    def _supplement(self, barrier, results, index):
        try:
            barrier.wait(timeout=10)
            results[index] = services.supplement_packet(
                packet_no=self.packet_no,
                submitter=self.user,
                files=[digest('并发补件.pdf', '两个线程相同的新内容')],
            )
        except Exception as exc:  # noqa: BLE001 - 断言需要真实异常
            results[index] = exc
        finally:
            connections.close_all()

    def test_concurrent_identical_files_serialize(self):
        barrier = threading.Barrier(2)
        results = [None, None]
        threads = [
            threading.Thread(target=self._supplement, args=(barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), '并发补件线程未在超时内完成')

        for item in results:
            self.assertNotIsInstance(item, Exception, f'并发提交出现异常: {item}')

        created = [r for r in results if r['version_created']]
        skipped = [r for r in results if not r['version_created']]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(len(skipped), 1, results)
        self.assertEqual(created[0]['files'][0]['result'], 'added')
        self.assertEqual(skipped[0]['files'][0]['result'], 'duplicate')

        # 版本号无空洞、无重复：v1、v2 各一
        numbers = list(
            IntakeVersion.objects.filter(packet__packet_no=self.packet_no)
            .values_list('version_no', flat=True)
        )
        self.assertEqual(sorted(numbers), [1, 2])
        self.assertEqual(
            IntakeAttachment.objects.filter(packet__packet_no=self.packet_no).count(), 2
        )
