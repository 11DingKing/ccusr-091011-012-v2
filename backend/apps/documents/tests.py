"""
文书档案与不可变摘要清单测试
"""
import hashlib
import threading

from django.db import connections
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from . import services
from .models import CustodyDocument, ImmutableRecordError, ManifestEntry, ManifestVersion


def digest_of(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def file_spec(name, content, size=None):
    return {
        'file_name': name,
        'file_size': len(content.encode('utf-8')) if size is None else size,
        'sha256': digest_of(content),
    }


class DocumentFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('doc-user', 'testpass123', role='admin')
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.document = CustodyDocument.objects.create(
            doc_no='JD-2026-0001', doc_type='appraisal',
            title='涉案服务器鉴定报告', created_by=self.user,
        )

    def receive(self, files, note=''):
        return self.client.post(
            f"/api/documents/{self.document.doc_no}/receipts/",
            {'files': files, 'note': note}, format='json',
        )


class DocumentAPITest(DocumentFixture):
    def test_create_document_and_reject_duplicate_doc_no(self):
        created = self.client.post('/api/documents/', {
            'doc_no': 'YJ-2026-0001', 'doc_type': 'transfer', 'title': '证物移交文书',
        }, format='json')
        duplicate = self.client.post('/api/documents/', {
            'doc_no': 'YJ-2026-0001', 'doc_type': 'transfer', 'title': '重复编号',
        }, format='json')
        self.assertEqual(created.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_create_document_validates_doc_no_and_type(self):
        bad_no = self.client.post('/api/documents/', {
            'doc_no': '非法 编号!', 'doc_type': 'transfer', 'title': 'x',
        }, format='json')
        bad_type = self.client.post('/api/documents/', {
            'doc_no': 'YJ-2026-0002', 'doc_type': 'unknown', 'title': 'x',
        }, format='json')
        self.assertEqual(bad_no.status_code, 400)
        self.assertEqual(bad_type.status_code, 400)

    def test_doc_no_is_immutable_and_reference_stays_stable(self):
        # 业务记录变更（改标题）后，按业务编号的引用保持稳定
        updated = self.client.put(f"/api/documents/{self.document.doc_no}/", {
            'title': '涉案服务器鉴定报告（修订）', 'remark': '补充说明',
        }, format='json')
        self.assertEqual(updated.status_code, 200)

        rejected = self.client.put(f"/api/documents/{self.document.doc_no}/", {
            'title': '试图改编号', 'doc_no': 'JD-2099-9999',
        }, format='json')
        self.assertEqual(rejected.status_code, 400)
        self.document.refresh_from_db()
        self.assertEqual(self.document.doc_no, 'JD-2026-0001')

        self.receive([file_spec('鉴定意见.pdf', 'appraisal-content')])
        detail = self.client.get(f"/api/documents/{self.document.doc_no}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['data']['current_version_no'], 1)
        missing = self.client.get('/api/documents/JD-2099-9999/')
        self.assertEqual(missing.status_code, 404)

    def test_requires_authentication(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get('/api/documents/').status_code, 401)
        self.assertEqual(
            anonymous.post(f"/api/documents/{self.document.doc_no}/receipts/", {}, format='json').status_code,
            401,
        )


class ReceiptTest(DocumentFixture):
    def test_first_receipt_creates_immutable_version(self):
        response = self.receive([
            file_spec('鉴定意见.pdf', 'content-a'),
            file_spec('检材清单.xlsx', 'content-b'),
        ], note='首次收件')

        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['result'], 'created')
        self.assertEqual(data['registered_count'], 2)

        version = data['version']
        self.assertEqual(version['version_no'], 1)
        self.assertEqual(version['reason'], 'initial')
        self.assertEqual(version['doc_no_snapshot'], 'JD-2026-0001')
        self.assertEqual(version['doc_title_snapshot'], '涉案服务器鉴定报告')
        self.assertEqual(version['submitted_by_name'], 'doc-user')
        self.assertEqual(version['prev_hash'], '')
        self.assertEqual(len(version['manifest_hash']), 64)

        entries = version['entries']
        self.assertEqual({e['file_name'] for e in entries}, {'鉴定意见.pdf', '检材清单.xlsx'})
        for entry in entries:
            self.assertEqual(entry['action'], 'add')
            self.assertEqual(len(entry['sha256']), 64)
            self.assertGreater(entry['file_size'], 0)

    def test_same_file_reupload_is_duplicate_without_new_version(self):
        first = self.receive([file_spec('鉴定意见.pdf', 'same-content')])
        second = self.receive([file_spec('鉴定意见.pdf', 'same-content')])

        self.assertEqual(first.json()['data']['result'], 'created')
        data = second.json()['data']
        self.assertEqual(data['result'], 'duplicate')
        self.assertIsNone(data['version'])
        self.assertEqual(len(data['duplicates']), 1)
        self.assertEqual(data['duplicates'][0]['duplicate_of'], 'active')
        self.assertEqual(ManifestVersion.objects.count(), 1)

    def test_same_content_different_name_is_also_duplicate(self):
        self.receive([file_spec('鉴定意见.pdf', 'same-content')])
        renamed = self.receive([file_spec('鉴定意见-副本.pdf', 'same-content')])

        data = renamed.json()['data']
        self.assertEqual(data['result'], 'duplicate')
        self.assertEqual(data['duplicates'][0]['existing_file_name'], '鉴定意见.pdf')
        self.assertEqual(ManifestVersion.objects.count(), 1)

    def test_partial_duplicate_registers_only_new_files(self):
        self.receive([file_spec('a.pdf', 'content-a')])
        response = self.receive([file_spec('a.pdf', 'content-a'), file_spec('b.pdf', 'content-b')])

        data = response.json()['data']
        self.assertEqual(data['result'], 'created')
        self.assertEqual(data['registered_count'], 1)
        self.assertEqual(len(data['duplicates']), 1)
        self.assertEqual(data['version']['entries'][0]['file_name'], 'b.pdf')

    def test_same_name_different_content_is_registered_and_flagged(self):
        self.receive([file_spec('鉴定意见.pdf', 'content-v1')])
        response = self.receive([file_spec('鉴定意见.pdf', 'content-v2')])

        data = response.json()['data']
        self.assertEqual(data['result'], 'created')
        self.assertEqual(data['registered_count'], 1)
        self.assertEqual(len(data['name_conflicts']), 1)
        self.assertEqual(data['name_conflicts'][0]['file_name'], '鉴定意见.pdf')

        # 同名不同内容的两个摘要都保留在当前有效清单中，互不覆盖
        current = self.client.get(f"/api/documents/{self.document.doc_no}/files/")
        self.assertEqual(current.json()['data']['total'], 2)

    def test_supplement_creates_new_version_with_hash_chain(self):
        self.receive([file_spec('a.pdf', 'content-a')])
        supplement = self.receive([file_spec('b.pdf', 'content-b')], note='补件')

        data = supplement.json()['data']
        self.assertEqual(data['version']['version_no'], 2)
        self.assertEqual(data['version']['reason'], 'supplement')

        v1 = ManifestVersion.objects.get(document=self.document, version_no=1)
        v2 = ManifestVersion.objects.get(document=self.document, version_no=2)
        self.assertEqual(v2.prev_hash, v1.manifest_hash)

        # 旧版本内容保持原样
        v1_detail = self.client.get(f"/api/documents/{self.document.doc_no}/versions/1/")
        self.assertEqual(len(v1_detail.json()['data']['entries']), 1)
        self.assertEqual(v1_detail.json()['data']['entries'][0]['file_name'], 'a.pdf')

    def test_version_snapshot_survives_document_update(self):
        self.receive([file_spec('a.pdf', 'content-a')])
        self.client.put(f"/api/documents/{self.document.doc_no}/", {'title': '新标题'}, format='json')

        detail = self.client.get(f"/api/documents/{self.document.doc_no}/versions/1/")
        version = detail.json()['data']
        self.assertEqual(version['doc_title_snapshot'], '涉案服务器鉴定报告')
        self.assertEqual(version['doc_no_snapshot'], 'JD-2026-0001')

    def test_receipt_validates_payload(self):
        missing_hash = self.receive([{'file_name': 'a.pdf', 'file_size': 10}])
        bad_hash = self.receive([{'file_name': 'a.pdf', 'file_size': 10, 'sha256': 'xyz'}])
        empty_files = self.receive([])
        negative_size = self.receive([file_spec('a.pdf', 'content-a', size=-1)])

        for response in (missing_hash, bad_hash, empty_files, negative_size):
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ManifestVersion.objects.count(), 0)

    def test_receipt_on_missing_document_returns_404(self):
        response = self.client.post('/api/documents/NO-SUCH/receipts/', {
            'files': [file_spec('a.pdf', 'content-a')],
        }, format='json')
        self.assertEqual(response.status_code, 404)


class InvalidationTest(DocumentFixture):
    def setUp(self):
        super().setUp()
        self.receive([file_spec('a.pdf', 'content-a'), file_spec('b.pdf', 'content-b')])
        self.entry_a = ManifestEntry.objects.get(file_name='a.pdf')
        self.entry_b = ManifestEntry.objects.get(file_name='b.pdf')

    def invalidate(self, entry_ids, note=''):
        return self.client.post(
            f"/api/documents/{self.document.doc_no}/invalidations/",
            {'entry_ids': entry_ids, 'note': note}, format='json',
        )

    def test_invalidation_creates_new_version_and_keeps_old_digest(self):
        response = self.invalidate([self.entry_a.id], note='送检单位撤回')

        data = response.json()['data']
        self.assertEqual(data['result'], 'created')
        self.assertEqual(data['version']['reason'], 'invalidation')
        self.assertEqual(data['version']['version_no'], 2)
        self.assertEqual(data['voided_entry_ids'], [self.entry_a.id])

        void_entry = data['version']['entries'][0]
        self.assertEqual(void_entry['action'], 'void')
        self.assertEqual(void_entry['voids'], self.entry_a.id)
        self.assertEqual(void_entry['sha256'], self.entry_a.sha256)

        # 原摘要未被覆盖，仍属于版本1
        self.entry_a.refresh_from_db()
        self.assertEqual(self.entry_a.action, 'add')
        verify = self.client.post(
            f"/api/documents/{self.document.doc_no}/versions/1/verify/",
            {'sha256': self.entry_a.sha256}, format='json',
        )
        self.assertTrue(verify.json()['data']['matched'])

        # 当前有效清单只剩 b.pdf
        current = self.client.get(f"/api/documents/{self.document.doc_no}/files/")
        names = [e['file_name'] for e in current.json()['data']['list']]
        self.assertEqual(names, ['b.pdf'])

    def test_repeated_invalidation_is_explicit_and_creates_no_version(self):
        self.invalidate([self.entry_a.id])
        again = self.invalidate([self.entry_a.id])

        data = again.json()['data']
        self.assertEqual(data['result'], 'already_voided')
        self.assertIsNone(data['version'])
        self.assertEqual(ManifestVersion.objects.count(), 2)

    def test_invalidation_rejects_unknown_or_foreign_entry(self):
        other_doc = CustodyDocument.objects.create(
            doc_no='YJ-2026-0001', doc_type='transfer', title='移交文书', created_by=self.user,
        )
        other = services.receive_files(
            document_id=other_doc.id, files=[file_spec('c.pdf', 'content-c')],
            note='', user=self.user,
        )
        foreign_id = other['entries'][0].id

        unknown = self.invalidate([999999])
        foreign = self.invalidate([foreign_id])

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(ManifestVersion.objects.filter(document=self.document).count(), 1)

    def test_voided_file_can_be_received_again(self):
        self.invalidate([self.entry_a.id])
        reupload = self.receive([file_spec('a.pdf', 'content-a')])

        data = reupload.json()['data']
        self.assertEqual(data['result'], 'created')
        self.assertEqual(data['version']['version_no'], 3)
        current = self.client.get(f"/api/documents/{self.document.doc_no}/files/")
        self.assertEqual(current.json()['data']['total'], 2)


class VerifyTest(DocumentFixture):
    def setUp(self):
        super().setUp()
        self.receive([file_spec('a.pdf', 'content-a')])
        self.receive([file_spec('b.pdf', 'content-b')])
        self.sha_a = digest_of('content-a')
        self.sha_b = digest_of('content-b')

    def test_verify_digest_belongs_to_specified_version(self):
        in_v1 = self.client.post(f"/api/documents/{self.document.doc_no}/versions/1/verify/", {
            'sha256': self.sha_a,
        }, format='json')
        not_in_v1 = self.client.post(f"/api/documents/{self.document.doc_no}/versions/1/verify/", {
            'sha256': self.sha_b,
        }, format='json')
        in_v2 = self.client.post(f"/api/documents/{self.document.doc_no}/versions/2/verify/", {
            'sha256': self.sha_b,
        }, format='json')

        self.assertTrue(in_v1.json()['data']['matched'])
        self.assertFalse(not_in_v1.json()['data']['matched'])
        self.assertTrue(in_v2.json()['data']['matched'])

    def test_verify_reports_field_mismatch(self):
        response = self.client.post(f"/api/documents/{self.document.doc_no}/versions/1/verify/", {
            'sha256': self.sha_a, 'file_name': '改名.pdf', 'file_size': 999,
        }, format='json')
        data = response.json()['data']
        self.assertTrue(data['matched'])
        self.assertEqual(sorted(data['mismatches']), ['file_name', 'file_size'])

    def test_verify_missing_version_returns_404(self):
        response = self.client.post(f"/api/documents/{self.document.doc_no}/versions/99/verify/", {
            'sha256': self.sha_a,
        }, format='json')
        self.assertEqual(response.status_code, 404)

    def test_verify_current_distinguishes_active_voided_unknown(self):
        entry_a = ManifestEntry.objects.get(sha256=self.sha_a)
        services.invalidate_entries(
            document_id=self.document.id, entry_ids=[entry_a.id], note='', user=self.user,
        )

        active = self.client.post(f"/api/documents/{self.document.doc_no}/verify/", {
            'sha256': self.sha_b,
        }, format='json')
        voided = self.client.post(f"/api/documents/{self.document.doc_no}/verify/", {
            'sha256': self.sha_a,
        }, format='json')
        unknown = self.client.post(f"/api/documents/{self.document.doc_no}/verify/", {
            'sha256': digest_of('never-seen'),
        }, format='json')

        self.assertEqual(active.json()['data']['status'], 'active')
        self.assertTrue(active.json()['data']['matched'])

        voided_data = voided.json()['data']
        self.assertEqual(voided_data['status'], 'voided')
        self.assertFalse(voided_data['matched'])
        self.assertEqual(voided_data['voided_in_version'], 3)

        self.assertEqual(unknown.json()['data']['status'], 'unknown')


class ImmutabilityTest(DocumentFixture):
    def setUp(self):
        super().setUp()
        self.receive([file_spec('a.pdf', 'content-a')])
        self.version = ManifestVersion.objects.get(document=self.document, version_no=1)
        self.entry = self.version.entries.first()

    def test_version_cannot_be_modified_or_deleted(self):
        self.version.note = '篡改'
        with self.assertRaises(ImmutableRecordError):
            self.version.save()
        with self.assertRaises(ImmutableRecordError):
            self.version.delete()

    def test_entry_cannot_be_modified_or_deleted(self):
        self.entry.file_name = '篡改.pdf'
        with self.assertRaises(ImmutableRecordError):
            self.entry.save()
        with self.assertRaises(ImmutableRecordError):
            self.entry.delete()

    def test_document_doc_no_cannot_be_changed_via_model(self):
        self.document.doc_no = 'JD-2099-9999'
        with self.assertRaises(ImmutableRecordError):
            self.document.save()

    def test_document_with_versions_cannot_be_deleted(self):
        from django.db.models.deletion import ProtectedError
        with self.assertRaises(ProtectedError):
            self.document.delete()

    def test_manifest_hash_is_recomputable(self):
        recomputed = services.compute_manifest_hash(
            doc_no=self.version.doc_no_snapshot,
            version_no=self.version.version_no,
            prev_hash=self.version.prev_hash,
            entries=[{
                'action': self.entry.action,
                'file_name': self.entry.file_name,
                'file_size': self.entry.file_size,
                'sha256': self.entry.sha256,
                'void_sha256': '',
            }],
        )
        self.assertEqual(recomputed, self.version.manifest_hash)


class ConcurrentReceiptTest(TransactionTestCase):
    """并发补件：版本号唯一连续，相同文件并发上传只有一个新版本。"""

    def setUp(self):
        self.user = User.objects.create_user('concurrent-user', 'testpass123', role='admin')
        self.document = CustodyDocument.objects.create(
            doc_no='JD-2026-1000', doc_type='appraisal', title='并发测试', created_by=self.user,
        )
        services.receive_files(
            document_id=self.document.id,
            files=[file_spec('initial.pdf', 'initial-content')],
            note='', user=self.user,
        )

    def _run_concurrently(self, payloads):
        barrier = threading.Barrier(len(payloads))
        results, errors = [], []

        def worker(files):
            try:
                barrier.wait(timeout=10)
                results.append(services.receive_files(
                    document_id=self.document.id, files=files, note='', user=self.user,
                ))
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results, errors

    def test_concurrent_supplements_get_distinct_sequential_versions(self):
        payloads = [[file_spec(f'f{i}.pdf', f'content-{i}')] for i in range(4)]
        results, errors = self._run_concurrently(payloads)

        self.assertEqual(errors, [])
        self.assertTrue(all(r['result'] == 'created' for r in results))

        version_nos = sorted(
            ManifestVersion.objects.filter(document=self.document)
            .values_list('version_no', flat=True)
        )
        self.assertEqual(version_nos, [1, 2, 3, 4, 5])

        # 哈希链完整：每个版本的 prev_hash 指向前一版本
        versions = ManifestVersion.objects.filter(document=self.document).order_by('version_no')
        for prev, curr in zip(versions, versions[1:]):
            self.assertEqual(curr.prev_hash, prev.manifest_hash)

        self.assertEqual(services.get_active_entries(self.document).count(), 5)

    def test_concurrent_identical_uploads_create_single_version(self):
        same_file = [file_spec('same.pdf', 'identical-content')]
        results, errors = self._run_concurrently([same_file, same_file])

        self.assertEqual(errors, [])
        outcomes = sorted(r['result'] for r in results)
        self.assertEqual(outcomes, ['created', 'duplicate'])
        self.assertEqual(ManifestVersion.objects.filter(document=self.document).count(), 2)
