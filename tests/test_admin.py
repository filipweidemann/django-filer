import os

import django
import django.core.files
from django.conf import settings
from django.contrib import admin
from django.contrib.admin import helpers
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.forms.models import model_to_dict as model_to_dict_django
from django.http import HttpRequest, HttpResponseForbidden
from django.test import RequestFactory, TestCase
from django.urls import reverse

from easy_thumbnails.files import get_thumbnailer
from easy_thumbnails.options import ThumbnailOptions

from filer import settings as filer_settings
from filer.admin import tools
from filer.admin.folderadmin import FolderAdmin
from filer.models.filemodels import File
from filer.models.foldermodels import Folder, FolderPermission
from filer.models.virtualitems import FolderRoot
from filer.settings import DEFERRED_THUMBNAIL_SIZES, FILER_IMAGE_MODEL
from filer.templatetags.filer_admin_tags import file_icon_url, get_aspect_ratio_and_download_url

from filer.thumbnail_processors import normalize_subject_location
from filer.utils.loader import load_model
from tests.helpers import SettingsOverride, create_folder_structure, create_image, create_superuser
from tests.utils.extended_app.models import ExtImage, Video


Image = load_model(FILER_IMAGE_MODEL)
User = get_user_model()


def model_to_dict(instance, **kwargs):
    if kwargs.pop('all'):
        kwargs['fields'] = [field.name for field in instance._meta.fields]
    return model_to_dict_django(instance, **kwargs)


class FilerFolderAdminUrlsTests(TestCase):
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')

    def tearDown(self):
        self.client.logout()

    def test_filer_app_index_get(self):
        response = self.client.get(reverse('admin:app_list', args=('filer',)))
        self.assertEqual(response.status_code, 200)

    def test_filer_make_root_folder_get(self):
        response = self.client.get(reverse('admin:filer-directory_listing-make_root_folder') + "?_popup=1")
        self.assertEqual(response.status_code, 200)

    def test_filer_make_root_folder_get_no_param(self):
        response = self.client.get(reverse('admin:filer-directory_listing-make_root_folder'))
        self.assertEqual(response.status_code, 200)

    def test_filer_make_root_folder_post(self):
        FOLDER_NAME = "root folder 1"
        self.assertEqual(Folder.objects.count(), 0)
        response = self.client.post(
            reverse('admin:filer-directory_listing-make_root_folder'), {
                "name": FOLDER_NAME,
            }
        )
        self.assertEqual(Folder.objects.count(), 1)
        self.assertEqual(Folder.objects.all()[0].name, FOLDER_NAME)
        # TODO: not sure why the status code is 200
        self.assertEqual(response.status_code, 200)

    def test_filer_remember_last_opened_directory(self):
        folder = Folder.objects.create(name='remember me please')

        get_last_folder = lambda: self.client.get(reverse('admin:filer-directory_listing-last'), follow=True)  # noqa

        self.client.get(reverse('admin:filer-directory_listing', kwargs={'folder_id': folder.id}))
        self.assertEqual(int(self.client.session['filer_last_folder_id']), folder.id)

        self.assertEqual(get_last_folder().context['folder'], folder)

        # let's test fallback
        folder.delete()
        self.assertTrue(isinstance(get_last_folder().context['folder'], FolderRoot))

    def test_filer_directory_listing_root_empty_get(self):
        response = self.client.get(reverse('admin:filer-directory_listing-root'))
        self.assertEqual(response.status_code, 200)

    def test_filer_directory_listing_root_get(self):
        create_folder_structure(depth=3, sibling=2, parent=None)
        response = self.client.get(reverse('admin:filer-directory_listing-root'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['folder'].children.count(), 6)

    def test_filer_directory_listing_performance(self):
        # Any number of images > then the number of allowed queries to ensure that images do not trigger individual
        # queries.
        images = 10

        thumbnail_urls = []
        for i in range(images):
            filename = f'test_image_{i}.jpg'
            os_filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, filename)
            create_image().save(os_filename, 'JPEG')
            with open(os_filename, 'rb') as f:
                file_obj = django.core.files.File(f, name=filename)
                image_obj = Image.objects.create(owner=self.superuser, original_filename=filename, file=file_obj, mime_type='image/jpeg')
                image_obj.save()
                thumbnailer = get_thumbnailer(image_obj)
                thumbnail_options = ThumbnailOptions({"size": (40, 40), "crop": True})
                thumbnail_urls.append(thumbnailer.get_thumbnail(thumbnail_options).url)

        self.assertEqual(Image.objects.count(), images)
        with self.assertNumQueries(7):
            # Expected queries:
            # 1. Authentication check
            # 2.-5. Loading the user clipboard
            # 6. Loading directory data and thumbnails (1 query)
            # 7. Selecting file and owner data
            response = self.client.get(reverse('admin:filer-directory_listing-unfiled_images'))
        self.assertContains(response, "test_image_0.jpg")
        self.assertContains(response, "/media/my-preferred-base-url-for-source-files/")
        self.assertContains(response, "/media/my-preferred-base-url-for-thumbnails/")
        for thumbnail_url in thumbnail_urls:
            self.assertContains(response, thumbnail_url)

    def test_validate_no_duplicate_folders(self):
        FOLDER_NAME = "root folder 1"
        self.assertEqual(Folder.objects.count(), 0)
        response = self.client.post(
            reverse('admin:filer-directory_listing-make_root_folder'), {
                "name": FOLDER_NAME,
                "_popup": 1
            }
        )
        self.assertEqual(Folder.objects.count(), 1)
        self.assertEqual(Folder.objects.all()[0].name, FOLDER_NAME)
        # and create another one
        response = self.client.post(
            reverse('admin:filer-directory_listing-make_root_folder'),
            {"name": FOLDER_NAME, "_popup": 1}
        )
        # second folder didn't get created
        self.assertEqual(Folder.objects.count(), 1)
        self.assertContains(response, 'Folder with this name already exists')

    def test_validate_no_duplicate_folders_on_rename(self):
        self.assertEqual(Folder.objects.count(), 0)
        response = self.client.post(
            reverse('admin:filer-directory_listing-make_root_folder'), {
                "name": "foo",
                "_popup": 1
            }
        )
        self.assertEqual(Folder.objects.count(), 1)
        self.assertEqual(Folder.objects.all()[0].name, "foo")
        # and create another one
        response = self.client.post(
            reverse('admin:filer-directory_listing-make_root_folder'), {
                "name": "bar",
                "_popup": 1
            }
        )
        self.assertEqual(Folder.objects.count(), 2)
        bar = Folder.objects.get(name="bar")
        admin_url = reverse("admin:filer_folder_change", args=(bar.pk, ))
        response = self.client.post(admin_url, {"name": "foo", "_popup": 1})
        self.assertContains(response, 'Folder with this name already exists')
        # refresh from db and validate that it's name didn't change
        bar = Folder.objects.get(pk=bar.pk)
        self.assertEqual(bar.name, "bar")

    def test_change_folder_owner_keep_name(self):
        folder = Folder.objects.create(name='foobar')
        another_superuser = User.objects.create_superuser(
            'gigi', 'admin@ignore.com', 'secret')
        admin_url = reverse("admin:filer_folder_change", args=(folder.pk, ))
        response = self.client.post(admin_url, {
            'owner': another_superuser.pk,
            'name': 'foobar',
            '_continue': 'Save and continue editing'
        })
        # successful POST returns a redirect
        self.assertEqual(response.status_code, 302)
        folder = Folder.objects.get(pk=folder.pk)
        self.assertEqual(folder.owner.pk, another_superuser.pk)

    def test_folder_admin_uses_admin_context(self):
        folder = Folder.objects.create(name='foo')
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': folder.id})
        response = self.client.get(url)
        self.assertTrue('site_header' in response.context)
        self.assertTrue('site_title' in response.context)

    def test_folder_list_actions(self):
        Folder.objects.create(name='foo')
        actions = [
            'Delete selected files and/or folders',
            'Move selected files and/or folders',
            'Copy selected files and/or folders',
            'Resize selected images',
            'Rename files',
        ]

        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=False):
            response = self.client.get(reverse('admin:filer-directory_listing-root'))

            for action in actions:
                self.assertContains(response, action)

        actions_with_permissions = [
            'Disable permissions for selected files',
            'Enable permissions for selected files',
        ]

        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            response = self.client.get(reverse('admin:filer-directory_listing-root'))

            for action in (actions_with_permissions + actions):
                self.assertContains(response, action)

    def test_filer_list_type_query_string(self):
        response = self.client.get(reverse('admin:filer-directory_listing-root') + "?_list_type=th")
        self.assertContains(response, 'navigator-thumbnail-list')

        response = self.client.get(reverse('admin:filer-directory_listing-root') + "?_list_type=tb")
        self.assertContains(response, 'navigator-table')

    def test_filer_list_type_setting(self):
        with SettingsOverride(filer_settings, FILER_FOLDER_ADMIN_DEFAULT_LIST_TYPE='th'):
            response = self.client.get(reverse('admin:filer-directory_listing-root'))
            self.assertContains(response, 'navigator-thumbnail-list')

    def test_filer_list_type_setting_when_user_set_wrong_choice(self):
        # If choice not exists then it should set table view as default
        with SettingsOverride(settings, FILER_FOLDER_ADMIN_DEFAULT_LIST_TYPE='qwerty'):
            # `settings` instead of `filer_settings` to give chance filer_settings
            # conditional to fire up
            response = self.client.get(reverse('admin:filer-directory_listing-root'))
            self.assertContains(response, 'navigator-table')

    def test_filer_list_type_setting_when_use_wrong_query_string_choice(self):
        # If list type in query string not exists show default list type
        with SettingsOverride(filer_settings, FILER_FOLDER_ADMIN_DEFAULT_LIST_TYPE='th'):
            response = self.client.get(reverse('admin:filer-directory_listing-root') + "?_list_type=qwerty")
            self.assertContains(response, 'navigator-thumbnail-list')


class FilerImageAdminUrlsTests(TestCase):
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')
        self.img = create_image()
        self.image_name = 'test_file.jpg'
        self.filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, self.image_name)
        self.img.save(self.filename, 'JPEG')
        with open(self.filename, 'rb') as upload:
            self.file_object = Image.objects.create(file=django.core.files.File(upload, name=self.image_name))

    def tearDown(self):
        self.client.logout()
        os.remove(self.filename)

    def test_icon_view_sizes(self):
        """Redirects are issued for accepted thumbnail sizes and 404 otherwise"""
        test_set = tuple((size, 302) for size in DEFERRED_THUMBNAIL_SIZES)
        test_set += (50, 404), (90, 404), (320, 404)
        for size, expected_status in test_set:
            url = reverse('admin:filer_file_fileicon', kwargs={
                'file_id': self.file_object.pk,
                'size': size,
            })
            response = self.client.get(url)
            self.assertEqual(response.status_code, expected_status)
            if response.status_code == 302:  # redirect
                # Redirects to a media file
                self.assertIn("/media/", response["Location"])
                # Does not redirect to a static file
                self.assertNotIn("/static/", response["Location"])

    def test_missing_file(self):
        """Directory shows static icon for missing files"""
        image = Image.objects.create(
            owner=self.superuser,
            original_filename="some-image.jpg",
        )
        url = reverse('admin:filer_file_fileicon', kwargs={
            'file_id': image.pk,
            'size': 80,
        })

        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("icons/file-missing.svg", response["Location"])

    def test_icon_view_non_image(self):
        """Getting an icon for a non-image results in a 404"""
        file = File.objects.create(
            owner=self.superuser,
            original_filename="some-file.xyz",
        )
        url = reverse('admin:filer_file_fileicon', kwargs={
            'file_id': file.pk,
            'size': 80,
        })

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_detail_view_missing_file(self):
        """Detail view shows static icon for missing file"""
        image = Image.objects.create(
            owner=self.superuser,
            original_filename="some-image.jpg",
        )
        image._width = 50
        image._height = 200
        image.save()

        url = reverse('admin:filer_image_change', kwargs={
            'object_id': image.pk,
        })

        response = self.client.get(url)

        self.assertContains(response, "icons/file-missing.svg")
        self.assertContains(response, 'width="210"')
        self.assertContains(response, 'height="210"')
        self.assertContains(response, 'alt="File is missing"')


class FilerClipboardAdminUrlsTests(TestCase):
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')
        self.img = create_image()
        self.image_name = 'test_file.jpg'
        self.filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, self.image_name)
        self.img.save(self.filename, 'JPEG')
        self.video = create_image()
        self.video_name = 'test_file.mov'
        self.video_filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, self.video_name)
        self.video.save(self.video_filename, 'JPEG')
        self.binary_name = 'aaa.bin'
        self.binary_filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, self.binary_name)
        with open(self.binary_filename, 'wb') as fh:
            fh.write(bytearray(100 * b'a'))
        super().setUp()

    def tearDown(self):
        self.client.logout()
        os.remove(self.filename)
        os.remove(self.video_filename)
        super().tearDown()

    def test_filer_upload_file(self, extra_headers={}):
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse('admin:filer-ajax_upload', kwargs={'folder_id': folder.pk})
            post_data = {
                'Filename': self.image_name,
                'Filedata': file_obj,
                'jsessionid': self.client.session.session_key
            }
            self.client.post(url, post_data, **extra_headers)

        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Image.objects.all()[0].original_filename,
                         self.image_name)

    def test_filer_upload_video(self, extra_headers={}):
        with SettingsOverride(filer_settings, FILER_FILE_MODELS=(
            'extended_app.ExtImage',
            'extended_app.Video',
            'filer.Image',
            'filer.File'
        )):
            self.assertEqual(Video.objects.count(), 0)
            folder = Folder.objects.create(name='foo')
            with open(self.video_filename, 'rb') as fh:
                file_obj = django.core.files.File(fh)
                url = reverse('admin:filer-ajax_upload', kwargs={'folder_id': folder.pk})
                post_data = {
                    'Filename': self.video_name,
                    'Filedata': file_obj,
                    'jsessionid': self.client.session.session_key
                }
                self.client.post(url, post_data, **extra_headers)

            self.assertEqual(Video.objects.count(), 1)
            self.assertEqual(Video.objects.all()[0].original_filename, self.video_name)

    def test_filer_upload_extimage(self, extra_headers={}):
        with SettingsOverride(filer_settings, FILER_FILE_MODELS=(
            'extended_app.ExtImage',
            'extended_app.Video',
            'filer.Image',
            'filer.File'
        )):
            self.assertEqual(ExtImage.objects.count(), 0)
            folder = Folder.objects.create(name='foo')
            with open(self.filename, 'rb') as fh:
                file_obj = django.core.files.File(fh)
                url = reverse('admin:filer-ajax_upload', kwargs={'folder_id': folder.pk})
                post_data = {
                    'Filename': self.image_name,
                    'Filedata': file_obj,
                    'jsessionid': self.client.session.session_key
                }
                self.client.post(url, post_data, **extra_headers)

            self.assertEqual(ExtImage.objects.count(), 1)
            self.assertEqual(ExtImage.objects.all()[0].original_filename, self.image_name)

    def test_filer_upload_file_no_folder(self, extra_headers={}):
        self.assertEqual(Image.objects.count(), 0)
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse('admin:filer-ajax_upload')
            post_data = {
                'Filename': self.image_name,
                'Filedata': file_obj,
                'jsessionid': self.client.session.session_key
            }
            response = self.client.post(url, post_data, **extra_headers)  # noqa
            self.assertEqual(Image.objects.count(), 1)
            stored_image = Image.objects.first()
            self.assertEqual(stored_image.original_filename, self.image_name)
            self.assertEqual(stored_image.mime_type, 'image/jpeg')

    def test_filer_upload_binary_data(self, extra_headers={}):
        self.assertEqual(File.objects.count(), 0)
        with open(self.binary_filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse('admin:filer-ajax_upload')
            post_data = {
                'Filename': self.binary_name,
                'Filedata': file_obj,
                'jsessionid': self.client.session.session_key
            }
            self.client.post(url, post_data, **extra_headers)
            self.assertEqual(Image.objects.count(), 0)
            self.assertEqual(File.objects.count(), 1)
            stored_file = File.objects.first()
            self.assertEqual(stored_file.original_filename, self.binary_name)
            self.assertEqual(stored_file.mime_type, 'application/octet-stream')

    def test_filer_ajax_upload_file(self):
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse(
                'admin:filer-ajax_upload',
                kwargs={'folder_id': folder.pk}
            ) + '?filename=%s' % self.image_name
            response = self.client.post(  # noqa
                url,
                data=file_obj.read(),
                content_type='image/jpeg',
                **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
            )
        self.assertEqual(Image.objects.count(), 1)
        stored_image = Image.objects.first()
        self.assertEqual(stored_image.original_filename, self.image_name)
        self.assertEqual(stored_image.mime_type, 'image/jpeg')

    def test_filer_ajax_upload_file_using_content_type(self):
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.binary_filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse(
                'admin:filer-ajax_upload',
                kwargs={'folder_id': folder.pk}
            ) + '?filename=renamed.pdf'
            self.client.post(
                url,
                data=file_obj.read(),
                content_type='application/pdf',
                **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
            )
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(File.objects.count(), 1)
        stored_file = File.objects.first()
        self.assertEqual(stored_file.original_filename, 'renamed.pdf')
        self.assertEqual(stored_file.mime_type, 'application/pdf')

    def test_filer_ajax_upload_file_no_folder(self):
        self.assertEqual(Image.objects.count(), 0)
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse(
                'admin:filer-ajax_upload'
            ) + '?filename=%s' % self.image_name
            self.client.post(
                url,
                data=file_obj.read(),
                content_type='image/jpeg',
                **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
            )
        self.assertEqual(Image.objects.count(), 1)
        stored_image = Image.objects.first()
        self.assertEqual(stored_image.original_filename, self.image_name)
        self.assertEqual(stored_image.mime_type, 'image/jpeg')

    def test_filer_upload_file_error(self, extra_headers={}):
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse('admin:filer-ajax_upload',
                          kwargs={'folder_id': folder.pk + 1})
            post_data = {
                'Filename': self.image_name,
                'Filedata': file_obj,
                'jsessionid': self.client.session.session_key
            }
            response = self.client.post(url, post_data, **extra_headers)
        from filer.admin.clipboardadmin import NO_FOLDER_ERROR
        self.assertContains(response, NO_FOLDER_ERROR)
        self.assertEqual(Image.objects.count(), 0)

    def test_filer_ajax_upload_file_error(self):
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)
            url = reverse(
                'admin:filer-ajax_upload',
                kwargs={
                    'folder_id': folder.pk + 1}
            ) + '?filename={0}'.format(self.image_name)
            response = self.client.post(
                url,
                data=file_obj.read(),
                content_type='application/octet-stream',
                **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
            )
        from filer.admin.clipboardadmin import NO_FOLDER_ERROR
        self.assertContains(response, NO_FOLDER_ERROR)
        self.assertEqual(Image.objects.count(), 0)

    def test_filer_upload_permissions_error(self, extra_headers={}):
        self.client.logout()
        staff_user = User.objects.create_user(
            username='joe_new', password='x', email='joe@mata.com')
        staff_user.is_staff = True
        staff_user.save()
        staff_user.user_permissions.add(*Permission.objects.filter(codename="add_file"))
        self.client.login(username='joe_new', password='x')
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)

            with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):

                # give permissions over BAR
                FolderPermission.objects.create(
                    folder=folder,
                    user=staff_user,
                    type=FolderPermission.THIS,
                    can_edit=FolderPermission.DENY,
                    can_read=FolderPermission.ALLOW,
                    can_add_children=FolderPermission.DENY)
                url = reverse('admin:filer-ajax_upload',
                              kwargs={'folder_id': folder.pk})
                post_data = {
                    'Filename': self.image_name,
                    'Filedata': file_obj,
                    'jsessionid': self.client.session.session_key
                }
                response = self.client.post(url, post_data, **extra_headers)

        from filer.admin.clipboardadmin import NO_PERMISSIONS_FOR_FOLDER
        self.assertContains(response, NO_PERMISSIONS_FOR_FOLDER)
        self.assertEqual(Image.objects.count(), 0)

    def test_filer_ajax_upload_without_permissions_error(self, extra_headers={}):
        """User without add_file permission cannot upload"""
        self.client.logout()
        staff_user = User.objects.create_user(
            username='joe_new', password='x', email='joe@mata.com')
        staff_user.is_staff = True
        staff_user.save()
        self.client.login(username='joe_new', password='x')
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)

            url = reverse(
                'admin:filer-ajax_upload',
                kwargs={
                    'folder_id': folder.pk}
            ) + '?filename={0}'.format(self.image_name)
            response = self.client.post(
                url,
                data=file_obj.read(),
                content_type='application/octet-stream',
                **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
            )

        from filer.admin.clipboardadmin import NO_PERMISSIONS

        self.assertContains(response, NO_PERMISSIONS)
        self.assertEqual(Image.objects.count(), 0)

    def test_filer_add_file_permissions(self, extra_headers={}):
        """Add_file permissions reflect in has_... methods of File and Folder classes"""
        self.client.logout()
        staff_user = User.objects.create_user(
            username='joe_new', password='x', email='joe@mata.com')
        staff_user.is_staff = True
        staff_user.save()
        self.client.login(username='joe_new', password='x')
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')

        file_data = django.core.files.base.ContentFile('some data')
        file_data.name = self.filename
        file = File.objects.create(
            owner=self.superuser,
            original_filename=self.filename,
            file=file_data,
            folder=folder
        )
        file.save()
        request = HttpRequest()
        setattr(request, "user", staff_user)

        self.assertEqual(folder.has_add_children_permission(request), False)
        self.assertEqual(file.has_add_children_permission(request), False)

    def test_filer_ajax_upload_permissions_error(self, extra_headers={}):
        self.client.logout()
        staff_user = User.objects.create_user(
            username='joe_new', password='x', email='joe@mata.com')
        staff_user.is_staff = True
        staff_user.save()
        staff_user.user_permissions.add(*Permission.objects.filter(codename="add_file"))
        self.client.login(username='joe_new', password='x')
        self.assertEqual(Image.objects.count(), 0)
        folder = Folder.objects.create(name='foo')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh)

            with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):

                # give permissions over BAR
                FolderPermission.objects.create(
                    folder=folder,
                    user=staff_user,
                    type=FolderPermission.THIS,
                    can_edit=FolderPermission.DENY,
                    can_read=FolderPermission.ALLOW,
                    can_add_children=FolderPermission.DENY)
                url = reverse(
                    'admin:filer-ajax_upload',
                    kwargs={
                        'folder_id': folder.pk}
                ) + '?filename={0}'.format(self.image_name)
                response = self.client.post(
                    url,
                    data=file_obj.read(),
                    content_type='application/octet-stream',
                    **{'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
                )

        from filer.admin.clipboardadmin import NO_PERMISSIONS_FOR_FOLDER
        self.assertContains(response, NO_PERMISSIONS_FOR_FOLDER)
        self.assertEqual(Image.objects.count(), 0)

    def test_templatetag_file_icon_url(self):
        filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR, 'invalid.svg')
        with open(filename, 'wb') as fh:
            fh.write(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" height="0" width="0"><circle cx="0" cy="0" r="0" stroke="black" stroke-width="3" fill="red" /></svg>')
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh, name=filename)
            image_obj = Image.objects.create(owner=self.superuser, original_filename=self.image_name, file=file_obj, mime_type='image/svg+xml')
            image_obj.save()
        url = file_icon_url(image_obj)
        self.assertEqual(url, '/static/filer/icons/file\\u002Dunknown.svg')


class BulkOperationsMixin:
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')
        self.img = create_image()
        self.image_name = 'test_file.jpg'
        self.filename = os.path.join(settings.FILE_UPLOAD_TEMP_DIR,
                                 self.image_name)
        self.img.save(self.filename, 'JPEG')
        self.create_src_and_dst_folders()
        self.folder = Folder.objects.create(name="root folder", parent=None)
        self.sub_folder1 = Folder.objects.create(name="sub folder 1", parent=self.folder)
        self.sub_folder2 = Folder.objects.create(name="sub folder 2", parent=self.folder)
        self.image_obj = self.create_image(self.src_folder)
        self.create_file(self.folder)
        self.create_file(self.folder)
        self.create_image(self.folder)
        self.create_image(self.sub_folder1)
        self.create_file(self.sub_folder1)
        self.create_file(self.sub_folder1)
        self.create_image(self.sub_folder2)
        self.create_image(self.sub_folder2)

    def tearDown(self):
        self.client.logout()
        os.remove(self.filename)
        for f in File.objects.all():
            f.delete()
        for folder in Folder.objects.all():
            folder.delete()

    def create_src_and_dst_folders(self):
        self.src_folder = Folder(name="Src", parent=None)
        self.src_folder.save()
        self.dst_folder = Folder(name="Dst", parent=None)
        self.dst_folder.save()

    def create_image(self, folder, filename=None):
        filename = filename or 'test_image.jpg'
        with open(self.filename, 'rb') as fh:
            file_obj = django.core.files.File(fh, name=filename)
            image_obj = Image.objects.create(owner=self.superuser, original_filename=self.image_name, file=file_obj, folder=folder, mime_type='image/jpeg')
            image_obj.save()
        return image_obj

    def create_file(self, folder, filename=None):
        filename = filename or 'test_file.dat'
        file_data = django.core.files.base.ContentFile('some data')
        file_data.name = filename
        file_obj = File.objects.create(owner=self.superuser, original_filename=filename, file=file_data, folder=folder)
        file_obj.save()
        return file_obj


class FolderAndFileSortingMixin(BulkOperationsMixin):
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')
        self.img = create_image()
        self.folder_1 = Folder(name='Pictures', parent=None)
        self.folder_1.save()
        self.nested_folder_2 = Folder(name='Nested 2', parent=self.folder_1)
        self.nested_folder_1 = Folder(name='Nested 1', parent=self.folder_1)
        self.nested_folder_1.save()
        self.nested_folder_2.save()
        self.create_file(folder=self.folder_1, filename='background.jpg')
        self.create_file(folder=self.folder_1, filename='A_Testfile.jpg')
        self.create_file(folder=self.folder_1, filename='Another_Test.jpg')
        newspaper_file = self.create_file(folder=self.folder_1, filename='Newspaper.pdf')
        newspaper_file.name = 'Zeitung'
        newspaper_file.save()
        renamed_file = self.create_file(folder=self.folder_1, filename='last_when_sorting_by_filename.jpg')
        renamed_file.name = 'A cute dog'
        renamed_file.save()

    def tearDown(self):
        self.client.logout()
        for f in File.objects.all():
            f.delete()
        for folder in Folder.objects.all():
            folder.delete()


class FilerFolderAndFileSortingTests(FolderAndFileSortingMixin, TestCase):
    # Assert that the folders are correctly sorted
    def test_filer_folder_sorting(self):
        response = self.client.get(reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.folder_1.pk
        }))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['folder_children'].count(), 2)
        self.assertEqual(response.context['folder_children'][0].name, 'Nested 1')
        self.assertEqual(response.context['folder_children'][1].name, 'Nested 2')

    # Default sorting should be alphabetically
    def test_filer_directory_listing_default_sorting(self):
        response = self.client.get(reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.folder_1.pk
        }))
        self.assertEqual(response.status_code, 200)
        # when using the default sort, the folder_files are of type `list`,
        # so we assert the length.
        self.assertEqual(len(response.context['folder_files']), 5)

        expected_filenames = ['A cute dog', 'A_Testfile.jpg', 'Another_Test.jpg', 'background.jpg', 'Zeitung']
        for index, expected_filename in enumerate(expected_filenames):
            self.assertEqual(str(response.context['folder_files'][index]), expected_filename)

    # Now, all columns with empty name should be alphabetically sorted by their filename,
    # after that, at the end of the list, all files with and explicit name should appear;
    # however, since we ONLY sort by name, the order of items without name is not defined
    # by their filename but rather by their creation date.
    # So, the order is expected to be ordered as they are created in the setUp method.
    def test_filer_directory_listing_sorting_with_order_by_param(self):
        response = self.client.get(reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.folder_1.pk
        }), {'order_by': 'name'})
        self.assertEqual(response.status_code, 200)
        # when using the default sort, the folder_files are of type `list`,
        # so we assert the length.
        self.assertEqual(len(response.context['folder_files']), 5)

        expected_filenames = ['background.jpg', 'A_Testfile.jpg', 'Another_Test.jpg', 'A cute dog', 'Zeitung']
        for index, expected_filename in enumerate(expected_filenames):
            self.assertEqual(str(response.context['folder_files'][index]), expected_filename)

    # Finally, we can define a fallback column to pass into `order_by` so that files without
    # any name are still sorted by something (in this case, their original_filename).
    # This should yield the expected order as well, but NOT the exact same order as the default sorting,
    # since we sort by name FIRST and all items with the same value again by original_filename.
    def test_filer_directory_listing_sorting_with_multiple_order_by_params(self):
        response = self.client.get(reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.folder_1.pk
        }), {'order_by': 'name,original_filename'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['folder_files']), 5)

        expected_filenames = ['A_Testfile.jpg', 'Another_Test.jpg', 'background.jpg', 'A cute dog', 'Zeitung']
        for index, expected_filename in enumerate(expected_filenames):
            self.assertEqual(str(response.context['folder_files'][index]), expected_filename)


class FilerBulkOperationsTests(BulkOperationsMixin, TestCase):
    def test_move_files_and_folders_action(self):
        # TODO: Test recursive (files and folders tree) move

        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 0)
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.src_folder.id,
        })
        response = self.client.post(url, {
            'action': 'move_files_and_folders',
            'post': 'yes',
            'destination': self.dst_folder.id,
            helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
        })
        self.assertEqual(self.src_folder.files.count(), 0)
        self.assertEqual(self.dst_folder.files.count(), 1)
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.dst_folder.id,
        })
        response = self.client.post(url, {  # noqa
            'action': 'move_files_and_folders',
            'post': 'yes',
            'destination': self.src_folder.id,
            helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
        })
        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 0)

    def test_validate_no_duplicate_folders_on_move(self):
        """Create the following folder hierarchy:
        root
          |
          |--foo
          |   |-bar
          |
          |--bar

        and try to move the outer bar in foo. This has to fail since it would result
        in two folders with the same name and parent.
        """
        root = Folder.objects.create(name='root', owner=self.superuser)
        foo = Folder.objects.create(name='foo', parent=root, owner=self.superuser)
        bar = Folder.objects.create(name='bar', parent=root, owner=self.superuser)
        foos_bar = Folder.objects.create(name='bar', parent=foo, owner=self.superuser)  # noqa
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': root.pk,
        })
        response = self.client.post(url, {  # noqa
            'action': 'move_files_and_folders',
            'post': 'yes',
            'destination': foo.pk,
            helpers.ACTION_CHECKBOX_NAME: 'folder-%d' % (bar.pk,),
        })
        # refresh from db and validate that it hasn't been moved
        bar = Folder.objects.get(pk=bar.pk)
        self.assertEqual(bar.parent.pk, root.pk)

    # TODO: Delete/refactor, deprecated since clipboard is deprecated
    # def test_move_to_clipboard_action(self):
    #     # TODO: Test recursive (files and folders tree) move
    #
    #     self.assertEqual(self.src_folder.files.count(), 1)
    #     self.assertEqual(self.dst_folder.files.count(), 0)
    #     url = reverse('admin:filer-directory_listing', kwargs={
    #         'folder_id': self.src_folder.id,
    #     })
    #     response = self.client.post(url, {
    #         'action': 'move_to_clipboard',
    #         helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
    #     })
    #     self.assertEqual(self.src_folder.files.count(), 0)
    #     self.assertEqual(self.dst_folder.files.count(), 0)
    #     clipboard = Clipboard.objects.get(user=self.superuser)
    #     self.assertEqual(clipboard.files.count(), 1)
    #     tools.move_files_from_clipboard_to_folder(clipboard, self.src_folder)
    #     tools.discard_clipboard(clipboard)
    #     self.assertEqual(clipboard.files.count(), 0)
    #     self.assertEqual(self.src_folder.files.count(), 1)

    def test_files_set_public_action(self):
        self.image_obj.is_public = False
        self.image_obj.save()
        self.assertEqual(self.image_obj.is_public, False)
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.src_folder.id,
        })

        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            response = self.client.post(url, {  # noqa
                'action': 'files_set_public',
                helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
            })
            self.image_obj = Image.objects.get(id=self.image_obj.id)
            self.assertEqual(self.image_obj.is_public, True)

    def test_files_set_private_action(self):
        self.image_obj.is_public = True
        self.image_obj.save()
        self.assertEqual(self.image_obj.is_public, True)
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.src_folder.id,
        })

        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            response = self.client.post(url, {  # noqa
                'action': 'files_set_private',
                helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
            })
            self.image_obj = Image.objects.get(id=self.image_obj.id)
            self.assertEqual(self.image_obj.is_public, False)
            self.image_obj.is_public = True
            self.image_obj.save()

    def test_copy_files_and_folders_action(self):
        # TODO: Test recursive (files and folders tree) copy

        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 0)
        self.assertEqual(self.image_obj.original_filename, 'test_file.jpg')
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.src_folder.id,
        })
        response = self.client.post(url, {
            'action': 'copy_files_and_folders',
            'post': 'yes',
            'suffix': 'test',
            'destination': self.dst_folder.id,
            helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
        })
        self.assertEqual(response.status_code, 302)

        # check if copying to the same folder gives 403
        response = self.client.post(url, {
            'action': 'copy_files_and_folders',
            'post': 'yes',
            'suffix': 'test',
            'destination': self.src_folder.id,
            helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (self.image_obj.id,),
        })
        self.assertEqual(response.status_code, 403)

        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 1)
        self.assertEqual(self.src_folder.files[0].id, self.image_obj.id)
        dst_image_obj = self.dst_folder.files[0]
        self.assertEqual(dst_image_obj.original_filename, 'test_filetest.jpg')

    def test_copy_folder_action(self):
        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 0)
        self.assertEqual(self.dst_folder.children.count(), 0)
        self.assertEqual(self.image_obj.original_filename, 'test_file.jpg')
        url = reverse('admin:filer-directory_listing-root')
        response = self.client.post(url, {
            'action': 'copy_files_and_folders',
            'post': 'yes',
            'suffix': 'test',
            'destination': self.dst_folder.id,
            helpers.ACTION_CHECKBOX_NAME: 'folder-%d' % (self.src_folder.id,),
        })
        self.assertEqual(response.status_code, 302)

        self.assertEqual(self.src_folder.files.count(), 1)
        self.assertEqual(self.dst_folder.files.count(), 0)
        self.assertEqual(self.dst_folder.children.count(), 1)
        copied_dir = self.dst_folder.children.first()
        dst_image_obj = copied_dir.files[0]
        self.assertEqual(copied_dir.name, self.src_folder.name)
        self.assertEqual(copied_dir.files.count(), 1)
        self.assertEqual(dst_image_obj.original_filename, 'test_filetest.jpg')

    def _do_test_rename(self, url, new_name, file_obj=None, folder_obj=None):
        """
        Helper to submit rename form and check renaming result.
        'new_name' should be a plain string, no formatting supported.
        """
        if file_obj is not None:
            checkbox_name = 'file-{}'.format(file_obj.id)
            files = [file_obj]
        elif folder_obj is not None:
            checkbox_name = 'folder-{}'.format(folder_obj.id)
            # files inside this folder, non-recursive
            files = File.objects.filter(folder=folder_obj)
        else:
            raise ValueError('file_obj or folder_obj is required')

        response = self.client.post(url, {
            'action': 'rename_files',
            'post': 'yes',
            'rename_format': new_name,
            helpers.ACTION_CHECKBOX_NAME: checkbox_name,
        })
        self.assertEqual(response.status_code, 302)

        for f in files:
            f = f._meta.model.objects.get(pk=f.pk)
            self.assertEqual(f.name, new_name)

    def test_action_rename_files(self):
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.image_obj.folder.id,
        })
        self._do_test_rename(
            url=url, new_name='New Name', file_obj=self.image_obj)

    def test_action_rename_files_in_folder(self):
        self.assertEqual(
            File.objects.filter(folder=self.sub_folder2).count(), 2)

        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.folder.id,
        })

        self._do_test_rename(
            url=url, new_name='New Name', folder_obj=self.sub_folder2)

    def test_rename_files_without_a_folder(self):
        url = reverse('admin:filer-directory_listing-unfiled_images')
        file_obj = self.create_file(folder=None)
        self._do_test_rename(url=url, new_name='New Name',
                             file_obj=file_obj)


class FilerDeleteOperationTests(BulkOperationsMixin, TestCase):
    def test_delete_files_or_folders_action(self):
        self.assertNotEqual(File.objects.count(), 0)
        self.assertNotEqual(Image.objects.count(), 0)
        self.assertNotEqual(Folder.objects.count(), 0)
        url = reverse('admin:filer-directory_listing-root')
        folders = []
        for folder in FolderRoot().children.all():
            folders.append('folder-%d' % (folder.id,))
        # this returns the confirmation for the admin action
        response = self.client.post(url, {
            'action': 'delete_files_or_folders',
            'post': 'no',
            helpers.ACTION_CHECKBOX_NAME: folders,
        })
        # this does the actual deleting
        response = self.client.post(url, {  # noqa
            'action': 'delete_files_or_folders',
            'post': 'yes',
            helpers.ACTION_CHECKBOX_NAME: folders,
        })
        self.assertEqual(File.objects.count(), 0)
        self.assertEqual(Folder.objects.count(), 0)

    def test_delete_files_or_folders_action_with_mixed_types(self):
        # add more files/images so we can test the polymorphic queryset with multiple types
        self.create_file(folder=self.src_folder)
        self.create_image(folder=self.src_folder)
        self.create_file(folder=self.src_folder)
        self.create_image(folder=self.src_folder)

        self.assertNotEqual(File.objects.count(), 0)
        self.assertNotEqual(Image.objects.count(), 0)
        url = reverse('admin:filer-directory_listing', args=(self.folder.id,))
        folders = []
        for f in File.objects.filter(folder=self.folder):
            folders.append('file-%d' % (f.id,))
        folders.append('folder-%d' % self.sub_folder1.id)
        response = self.client.post(url, {  # noqa
            'action': 'delete_files_or_folders',
            'post': 'yes',
            helpers.ACTION_CHECKBOX_NAME: folders,
        })
        self.assertEqual(File.objects.filter(folder__in=[self.folder.id, self.sub_folder1.id]).count(), 0)


class FilerResizeOperationTests(BulkOperationsMixin, TestCase):
    # TODO: Test recursive (files and folders tree) processing.
    # The image object we test on has resolution of 800x600 with
    # subject location at (100, 200).
    def _test_resize_image(self, crop,
                           target_width, target_height,
                           expected_width, expected_height,
                           expected_subj_x, expected_subj_y):
        image_obj = self.create_image(self.src_folder)
        self.assertEqual(image_obj.width, 800)
        self.assertEqual(image_obj.height, 600)
        image_obj.subject_location = '100,200'
        image_obj.save()
        url = reverse('admin:filer-directory_listing', kwargs={
            'folder_id': self.src_folder.id,
        })
        response = self.client.post(url, {
            'action': 'resize_images',
            'post': 'yes',
            'width': target_width,
            'height': target_height,
            'crop': crop,
            'upscale': False,
            helpers.ACTION_CHECKBOX_NAME: 'file-%d' % (image_obj.id,),
        })
        self.assertEqual(response.status_code, 302)
        image_obj = Image.objects.get(id=image_obj.id)
        self.assertEqual(image_obj.width, expected_width)
        self.assertEqual(image_obj.height, expected_height)
        self.assertEqual(
            normalize_subject_location(image_obj.subject_location),
            (expected_subj_x, expected_subj_y))

    def test_resize_images_no_custom_processors(self):
        """Test bulk image resize action without custom template processors"""
        for thumbnail_processor in (
                'easy_thumbnails.processors.scale_and_crop',
                'filer.thumbnail_processors.scale_and_crop_with_subject_location'):
            with SettingsOverride(settings,
                                  THUMBNAIL_PROCESSORS=(
                                      'easy_thumbnails.processors.colorspace',
                                      'easy_thumbnails.processors.autocrop',
                                      thumbnail_processor,
                                      'easy_thumbnails.processors.filters',
                                  )):
                # without crop
                self._test_resize_image(
                    crop=False,
                    target_width=400, target_height=60,
                    expected_width=80, expected_height=60,   # height scale (0.1) is used
                    expected_subj_x=10, expected_subj_y=20,  # scale * original position
                )
                self._test_resize_image(
                    crop=False,
                    target_width=40, target_height=300,
                    expected_width=40, expected_height=30,   # width scale (0.05) is used
                    expected_subj_x=5, expected_subj_y=10,   # scale * original position
                )

                # with crop
                self._test_resize_image(
                    crop=True,
                    target_width=40, target_height=300,
                    expected_width=40, expected_height=300,
                    expected_subj_x=20, expected_subj_y=150,  # at the center
                )


class PermissionAdminTest(TestCase):
    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')

    def tearDown(self):
        self.client.logout()

    def test_render_add_view(self):
        """
        Really stupid and simple test to see if the add Permission view can be rendered
        """
        response = self.client.get(reverse('admin:filer_folderpermission_add'))
        self.assertEqual(response.status_code, 200)


class FolderListingTest(TestCase):

    def setUp(self):
        superuser = create_superuser()
        self.staff_user = User.objects.create_user(
            username='joe', password='x', email='joe@mata.com')
        self.staff_user.is_staff = True
        self.staff_user.save()
        perms = Permission.objects.filter(codename__in=["view_folder", "add_file", "add_folder", "can_use_directory_listing"])
        self.staff_user.user_permissions.add(*perms)
        self.parent = Folder.objects.create(name='bar', parent=None, owner=superuser)

        self.foo_folder = Folder.objects.create(name='foo', parent=self.parent, owner=self.staff_user)
        self.bar_folder = Folder.objects.create(name='bar', parent=self.parent, owner=superuser)
        self.baz_folder = Folder.objects.create(name='baz', parent=self.parent, owner=superuser)

        file_data = django.core.files.base.ContentFile('some data')
        file_data.name = 'spam'
        self.spam_file = File.objects.create(
            owner=superuser, original_filename='spam',
            file=file_data, folder=self.parent)
        self.client.login(username='joe', password='x')

    def test_with_without_permissions(self):
        staff_user_wo_permissions = User.objects.create_user(
            username='joemata', password='x', email='joe@mata.com')
        staff_user_wo_permissions.is_staff = True
        staff_user_wo_permissions.save()
        self.client.login(username='joemata', password='x')
        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=False):
            response = self.client.get(
                reverse('admin:filer-directory_listing',
                        kwargs={'folder_id': self.parent.id}))
        self.assertIsInstance(response, HttpResponseForbidden)

    def test_with_permissions_disabled(self):
        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=False):
            response = self.client.get(
                reverse('admin:filer-directory_listing',
                        kwargs={'folder_id': self.parent.id}))
            item_list = response.context['paginated_items'].object_list
            # user sees all items: FOO, BAR, BAZ, SAMP
            self.assertEqual(
                set(folder.pk for folder in item_list),
                set([self.foo_folder.pk, self.bar_folder.pk, self.baz_folder.pk,
                     self.spam_file.pk]))

    def test_folder_ownership(self):
        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            response = self.client.get(
                reverse('admin:filer-directory_listing',
                        kwargs={'folder_id': self.parent.id}))
            item_list = response.context['paginated_items'].object_list
            # user sees only 1 folder : FOO
            # he doesn't see BAR, BAZ and SPAM because he doesn't own them
            # and no permission has been given
            self.assertEqual(
                set(folder.pk for folder in item_list),
                set([self.foo_folder.pk]))

    def test_with_permission_given_to_folder(self):
        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            # give permissions over BAR
            FolderPermission.objects.create(
                folder=self.bar_folder,
                user=self.staff_user,
                type=FolderPermission.THIS,
                can_edit=FolderPermission.ALLOW,
                can_read=FolderPermission.ALLOW,
                can_add_children=FolderPermission.ALLOW)
            response = self.client.get(
                reverse('admin:filer-directory_listing',
                        kwargs={'folder_id': self.parent.id}))
            item_list = response.context['paginated_items'].object_list
            # user sees 2 folder : FOO, BAR
            self.assertEqual(
                set(folder.pk for folder in item_list),
                set([self.foo_folder.pk, self.bar_folder.pk]))

    def test_with_permission_given_to_parent_folder(self):
        with SettingsOverride(filer_settings, FILER_ENABLE_PERMISSIONS=True):
            FolderPermission.objects.create(
                folder=self.parent,
                user=self.staff_user,
                type=FolderPermission.CHILDREN,
                can_edit=FolderPermission.ALLOW,
                can_read=FolderPermission.ALLOW,
                can_add_children=FolderPermission.ALLOW)
            response = self.client.get(
                reverse('admin:filer-directory_listing',
                        kwargs={'folder_id': self.parent.id}))
            item_list = response.context['paginated_items'].object_list
            # user sees all items because he has permissions on the parent folder
            self.assertEqual(
                set(folder.pk for folder in item_list),
                set([self.foo_folder.pk, self.bar_folder.pk, self.baz_folder.pk,
                     self.spam_file.pk]))

    def test_search_against_owner(self):
        url = reverse('admin:filer-directory_listing',
                      kwargs={'folder_id': self.parent.id})

        response = self.client.get(url, {'q': 'joe'})
        item_list = response.context['paginated_items'].object_list
        self.assertEqual(len(item_list), 1)

        response = self.client.get(url, {'q': 'admin'})
        item_list = response.context['paginated_items'].object_list
        self.assertEqual(len(item_list), 4)

    def test_owner_search_fields(self):
        folderadmin = FolderAdmin(Folder, admin.site)
        self.assertEqual(folderadmin.owner_search_fields, ['username', 'first_name', 'last_name', 'email'])

        folder_qs = folderadmin.filter_folder(Folder.objects.all(), ['joe@mata.com'])
        self.assertEqual(len(folder_qs), 1)

        class DontSearchOwnerEmailFolderAdmin(FolderAdmin):
            owner_search_fields = ['username', 'first_name', 'last_name']

        folderadmin = DontSearchOwnerEmailFolderAdmin(Folder, admin.site)

        folder_qs = folderadmin.filter_folder(Folder.objects.all(), ['joe@mata.com'])
        self.assertEqual(len(folder_qs), 0)

    def test_search_special_characters(self):
        """
        Regression test for https://github.com/divio/django-filer/pull/945.
        Because of a wrong unquoting function being used, searches with
        some "_XX" sequences got unquoted as unicode characters.
        For example, "_ec" gets unquoted as u'ì'.
        """
        url = reverse('admin:filer-directory_listing',
                      kwargs={'folder_id': self.parent.id})

        # Create a file with a problematic filename
        problematic_file = django.core.files.base.ContentFile('some data')
        filename = u'christopher_eccleston'
        problematic_file.name = filename
        self.spam_file = File.objects.create(
            owner=self.staff_user, original_filename=filename,
            file=problematic_file, folder=self.parent)

        # Valid search for the filename, should have one result
        response = self.client.get(url, {'q': filename})
        item_list = response.context['paginated_items'].object_list
        self.assertEqual(len(item_list), 1)


class FilerAdminContextTests(TestCase, BulkOperationsMixin):
    def setUp(self):
        BulkOperationsMixin.setUp(self)
        self.client.login(username='admin', password='secret')

    def tearDown(self):
        self.client.logout()

    def test_pick_mode_folder_delete(self):
        folder = Folder.objects.create(name='foo')
        base_url = reverse('admin:filer_folder_delete', args=[folder.id])
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(pick_url, data={'_popup': '1', 'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-root'
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_folder_delete(self):
        folder = Folder.objects.create(name='foo')
        base_url = reverse('admin:filer_folder_delete', args=[folder.id])

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(base_url, data={'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-root'
            )
        )

    def test_pick_mode_folder_with_parent_delete(self):
        parent_folder = Folder.objects.create(name='parent')
        folder = Folder.objects.create(name='foo', parent=parent_folder)
        base_url = reverse('admin:filer_folder_delete', args=[folder.id])
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(pick_url, data={'_popup': '1', 'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_folder_with_parent_delete(self):
        parent_folder = Folder.objects.create(name='parent')
        folder = Folder.objects.create(name='foo', parent=parent_folder)
        base_url = reverse('admin:filer_folder_delete', args=[folder.id])

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(base_url, data={'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            )
        )

    def test_pick_mode_image_delete(self):
        image = self.create_image(folder=None)
        base_url = image.get_admin_delete_url()
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(pick_url, data={
            '_popup': '1', 'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-unfiled_images'''
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_image_delete(self):
        image = self.create_image(folder=None)
        base_url = image.get_admin_delete_url()

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(base_url, data={'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-unfiled_images')
        )

    def test_pick_mode_image_with_folder_delete(self):
        parent_folder = Folder.objects.create(name='parent')
        image = self.create_image(folder=parent_folder)
        base_url = image.get_admin_delete_url()
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(pick_url,
                                    data={'_popup': '1', 'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_image_with_folder_delete(self):
        parent_folder = Folder.objects.create(name='parent')
        image = self.create_image(folder=parent_folder)
        base_url = image.get_admin_delete_url()

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(base_url, data={'post': 'yes'})
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            )
        )

    def test_pick_mode_image_save(self):
        image = self.create_image(folder=None)
        base_url = image.get_admin_change_url()
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<input type="hidden" name="_pick" value="file"')
        self.assertContains(response, '<input type="hidden" name="_popup" value="1"')
        data = {'_popup': '1'}
        data.update(model_to_dict(image, all=True))
        # Django 2.2
        # To catch usage mistakes, the test Client and django.utils.http.urlencode()
        # now raise TypeError if None is passed as a value to encode because None can’t
        # be encoded in GET and POST data. Either pass an empty string or omit the value.
        data = {k: v if v is not None else '' for k, v in data.items()}

        response = self.client.post(pick_url, data=data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-unfiled_images'
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_image_save(self):
        image = self.create_image(folder=None)
        base_url = image.get_admin_change_url()

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)
        data = model_to_dict(image, all=True)
        data = {k: v if v is not None else '' for k, v in data.items()}
        response = self.client.post(base_url, data=data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-unfiled_images'
            )
        )

    def test_image_subject_location(self):
        def do_test_image_subject_location(subject_location=None,
                                           should_succeed=True):
            # image is 800x600
            image = self.create_image(folder=None)
            base_url = image.get_admin_change_url()
            data = model_to_dict(image, all=True)
            data = {k: v if v is not None else '' for k, v in data.items()}

            if subject_location is not None:
                data.update(dict(subject_location=subject_location))

            response = self.client.post(base_url, data=data)
            saved_image = Image.objects.get(pk=image.pk)
            if should_succeed:
                self.assertRedirects(
                    response=response,
                    expected_url=reverse(
                        'admin:filer-directory_listing-unfiled_images'))
                self.assertEqual(saved_image.subject_location,
                                 subject_location)
            else:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(saved_image.subject_location,
                                 image.subject_location)

        for subject_location in '', '10,10', '800,0', '0,600', '800,600':
            do_test_image_subject_location(subject_location=subject_location)

        for subject_location in '-1,1', '801,0', '801,601':
            do_test_image_subject_location(subject_location=subject_location,
                                           should_succeed=False)

    def test_pick_mode_image_with_folder_save(self):
        parent_folder = Folder.objects.create(name='parent')
        image = self.create_image(folder=parent_folder)
        base_url = image.get_admin_change_url()
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)

        response.render()
        self.assertContains(response,
                            '<input type="hidden" name="_pick" value="file"')
        self.assertContains(response,
                            '<input type="hidden" name="_popup" value="1"')
        data = {'_popup': '1'}
        data.update(model_to_dict(image, all=True))
        data = {k: v if v is not None else '' for k, v in data.items()}
        response = self.client.post(pick_url, data=data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_image_with_folder_save(self):
        parent_folder = Folder.objects.create(name='parent')
        image = self.create_image(folder=parent_folder)
        base_url = image.get_admin_change_url()

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        data = model_to_dict(image, all=True)
        data = {k: v if v is not None else '' for k, v in data.items()}

        response = self.client.post(base_url, data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            )
        )

    def test_edit_from_widget_mode_save(self):
        parent_folder = Folder.objects.create(name='parent')
        image = self.create_image(folder=parent_folder)
        base_url = image.get_admin_change_url()
        edit_popup_url = base_url + '?_edit_from_widget=1&_popup=1'

        response = self.client.get(edit_popup_url)
        self.assertEqual(response.status_code, 200)
        response.render()
        self.assertContains(response,
                            '<input type="hidden" name="_popup" value="1"')
        self.assertContains(response,
                            '<input type="hidden" name="_edit_from_widget" value="1"')

        data = {'_popup': '1', '_edit_from_widget': '1'}
        data.update(model_to_dict(image, all=True))
        data = {k: v if v is not None else '' for k, v in data.items()}

        response = self.client.post(edit_popup_url, data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('media', response.context_data)
        response.render()
        self.assertContains(response, 'popup_response.js')

    def test_pick_mode_folder_save(self):
        folder = Folder.objects.create(name='foo')
        base_url = reverse('admin:filer_folder_change', args=[folder.id])
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            '<input type="hidden" name="_pick" value="file"')
        self.assertContains(response,
                            '<input type="hidden" name="_popup" value="1"')
        data = {
            '_popup': '1',
            'name': 'foobar',
        }
        response = self.client.post(pick_url, data=data)
        if response.status_code == 200:
            from pprint import pprint
            pprint(response.content)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-root'
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_folder_save(self):
        folder = Folder.objects.create(name='foo')
        base_url = reverse('admin:filer_folder_change', args=[folder.id])

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)
        data = {
            'name': 'foobar',
        }
        response = self.client.post(base_url, data=data)
        if response.status_code == 200:
            from pprint import pprint
            pprint(response.content)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-root'
            )
        )

    def test_pick_mode_folder_with_parent_save(self):
        parent_folder = Folder.objects.create(name='parent')
        folder = Folder.objects.create(name='foo', parent=parent_folder)
        base_url = reverse('admin:filer_folder_change', args=[folder.id])
        pick_url = base_url + '?_pick=file&_popup=1'

        response = self.client.get(pick_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            '<input type="hidden" name="_pick" value="file"')
        self.assertContains(response,
                            '<input type="hidden" name="_popup" value="1"')
        data = {
            '_popup': '1',
            'name': 'foobar',
        }
        response = self.client.post(pick_url, data=data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            ) + '?_pick=file&_popup=1'
        )

    def test_regular_mode_folder_with_parent_save(self):
        parent_folder = Folder.objects.create(name='parent')
        folder = Folder.objects.create(name='foo', parent=parent_folder)
        base_url = reverse('admin:filer_folder_change', args=[folder.id])

        response = self.client.get(base_url)
        self.assertEqual(response.status_code, 200)

        data = {
            'name': 'foobar',
        }
        response = self.client.post(base_url, data=data)
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing',
                args=[parent_folder.id]
            )
        )


class PolymorphicDeleteViewTests(BulkOperationsMixin, TestCase):
    def test_can_delete_mixed_file_and_image_items(self):
        """
        we need to use a patched version of the get_deleted_objects so it works
        with polymorphic models.
        see filer.admin.patched.admin_utils.get_deleted_objects
        """
        folder = Folder.objects.create(name='a folder with files and images inside')
        self.create_image(folder=folder, filename="i-am-a-image.jpg")
        self.create_file(folder=folder, filename="i-am-a-file.bin")
        self.assertEqual(Folder.objects.filter(id=folder.id).count(), 1)

        response = self.client.get(folder.get_admin_delete_url())
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            folder.get_admin_delete_url(),
            {
                'post': 'yes',
            }
        )
        self.assertRedirects(
            response=response,
            expected_url=reverse(
                'admin:filer-directory_listing-root'
            )
        )
        self.assertEqual(Folder.objects.filter(id=folder.id).count(), 0)


class AdminToolsTests(TestCase):

    def setUp(self):
        self.superuser = create_superuser()
        self.client.login(username='admin', password='secret')

    def tearDown(self):
        self.client.logout()

    def test_admin_url_params(self):
        request_factory = RequestFactory()
        request = request_factory.get('/')
        self.assertDictEqual(tools.admin_url_params(request), {})
        request = request_factory.get('/', {'_popup': '1', '_pick': 'file', '_edit_from_widget': '1'})
        self.assertDictEqual(tools.admin_url_params(request, {'extra_param': 42}), {
            '_popup': '1',
            '_pick': 'file',
            '_edit_from_widget': '1',
            'extra_param': 42,
        })
        request = request_factory.get('/', {'_pick': 'bad_type'})
        self.assertDictEqual(tools.admin_url_params(request), {})


class FileIconContextTests(TestCase):

    def test_image_icon_with_size(self):
        """
        Image with get an aspect ratio and will be present in context
        """
        image = Image.objects.create(name='test.jpg')
        image._width = 50
        image._height = 200
        image.save()
        context = {}
        height, width, context = get_aspect_ratio_and_download_url(context=context, detail=True, file=image, height=40, width=40)
        self.assertIn('sidebar_image_ratio', context.keys())
        self.assertIn('download_url', context.keys())

    def test_file_icon_with_size(self):
        """
        File with not get an aspect ratio and will not be present in context
        """
        file = File.objects.create(name='test.pdf')
        context = {}
        height, width, context = get_aspect_ratio_and_download_url(context=context, detail=True, file=file, height=40, width=40)
        self.assertNotIn('sidebar_image_ratio', context.keys())
        self.assertIn('download_url', context.keys())
