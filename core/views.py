import os.path
import structlog
from dateutil.parser import parse

from django.conf import settings
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import caches
from django.http import Http404, HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from .asciidoc import process_adoc_to_html_content
from .boostrenderer import get_content_from_s3
from .markdown import process_md
from .models import RenderedContent
from .tasks import (
    clear_rendered_content_cache_by_cache_key,
    clear_rendered_content_cache_by_content_type,
    refresh_content_from_s3,
    save_rendered_content,
)

logger = structlog.get_logger()


class ClearCacheView(UserPassesTestMixin, View):
    http_method_names = ["get"]
    login_url = "/login/"

    def get(self, request, *args, **kwargs):
        """Clears the redis and database cache for given parameters.

        Params (must pass one):
            content_type: The content type to clear. Example: "text/asciidoc"
            cache_key: The cache key to clear.
        """
        content_type = self.request.GET.get("content_type")
        cache_key = self.request.GET.get("cache_key")
        if not content_type and not cache_key:
            return HttpResponseNotFound()

        if content_type:
            clear_rendered_content_cache_by_content_type.delay(content_type)

        if cache_key:
            clear_rendered_content_cache_by_cache_key.delay(cache_key)

        return HttpResponse("Cache cleared")

    def handle_no_permission(self):
        """Handle a user without permission to access this page."""
        return HttpResponse(
            "You do not have permission to access this page.", status=403
        )

    def test_func(self):
        """Check if the user is a staff member"""
        return self.request.user.is_staff


class MarkdownTemplateView(TemplateView):
    template_name = "markdown_template.html"
    content_dir = settings.BASE_CONTENT

    def build_path(self):
        """
        Builds the path from URL kwargs
        """
        content_path = self.kwargs.get("content_path")

        if not content_path:
            return

        # If the request includes the file extension, return that
        if content_path[-5:] == ".html" or content_path[-3:] == ".md":
            return f"{self.content_dir}/{content_path}"

        # Trim any trailing slashes
        if content_path[-1] == "/":
            content_path = content_path[:-1]

        # Can we find a markdown file with this path?
        path = f"{self.content_dir}/{content_path}.md"

        # Note: The get() method also checks isfile(), but since we need to try multiple
        # paths/extensions, we need to call it here as well.
        if os.path.isfile(path):
            return path

        # Can we find an HTML file with this path?
        path = f"{self.content_dir}/{content_path}.html"
        if os.path.isfile(path):
            return path

        # Can we find an index file with this path?
        path = f"{self.content_dir}/{content_path}/index.html"
        if os.path.isfile(path):
            return path

        # If we get here, there is nothing else for us to try.
        return

    def get(self, request, *args, **kwargs):
        """
        Verifies the file and returns the frontmatter and content
        """
        path = self.build_path()

        # Avoids a TypeError from os.path.isfile if there is no path
        if not path:
            logger.info(
                "markdown_template_view_no_valid_path",
                content_path=kwargs.get("content_path"),
                status_code=404,
            )
            raise Http404("Page not found")

        if not os.path.isfile(path):
            logger.info(
                "markdown_template_view_no_valid_file",
                content_path=kwargs.get("content_path"),
                path=path,
                status_code=404,
            )
            raise Http404("Post not found")

        context = {}
        context["frontmatter"], context["content"] = process_md(path)
        logger.info(
            "markdown_template_view_success",
            content_path=kwargs.get("content_path"),
            path=path,
            status_code=200,
        )
        return self.render_to_response(context)


class ContentNotFoundException(Exception):
    pass


class StaticContentTemplateView(TemplateView):
    template_name = "adoc_content.html"

    def get(self, request, *args, **kwargs):
        """Returns static content that originates in S3, but is cached in a couple of
        different places.

        Any valid S3 key to the S3 bucket apecified in settings can be returned by
        this view. Pages like the Help page are stored in S3 and rendered via
        this view, for example.

        See the *_static_config.json files for URL mappings to specific S3 keys.
        """
        content_path = self.kwargs.get("content_path")
        try:
            self.content_dict = self.get_content(content_path)
        except ContentNotFoundException:
            logger.info(
                "get_content_from_s3_view_not_in_cache",
                content_path=content_path,
                status_code=404,
            )
            return HttpResponseNotFound("Page not found")
        return super().get(request, *args, **kwargs)

    def cache_result(self, static_content_cache, cache_key, result):
        static_content_cache.set(cache_key, result)

    def get_content(self, content_path):
        """Returns content from cache, database, or S3"""
        static_content_cache = caches["static_content"]
        cache_key = f"static_content_{content_path}"
        result = self.get_from_cache(static_content_cache, cache_key)

        if result is None:
            result = self.get_from_database(cache_key)
            if result:
                # When we get a result from the database, we refresh its content
                refresh_content_from_s3.delay(content_path, cache_key)

        if result is None:
            result = self.get_from_s3(content_path)
            if result:
                # Save to database
                self.save_to_database(cache_key, result)
                # Cache the result
                self.cache_result(static_content_cache, cache_key, result)

        if result is None:
            logger.info(
                "get_content_from_s3_view_no_valid_object",
                key=content_path,
                status_code=404,
            )
            raise ContentNotFoundException("Content not found")

        return result

    def get_context_data(self, **kwargs):
        """Returns the content and content type for the template. In some cases,
        changes the content type."""
        context = super().get_context_data(**kwargs)
        content_type = self.content_dict.get("content_type")
        content = self.content_dict.get("content")

        if content_type == "text/asciidoc":
            content_type = "text/html"

        context.update({"content": content, "content_type": content_type})

        logger.info(
            "get_content_from_s3_view_success", key=self.kwargs.get("content_path")
        )

        return context

    def get_from_cache(self, static_content_cache, cache_key):
        cached_result = static_content_cache.get(cache_key)
        return cached_result if cached_result else None

    def get_from_database(self, cache_key):
        try:
            content_obj = RenderedContent.objects.get(cache_key=cache_key)
            return {
                "content": content_obj.content_html,
                "content_type": content_obj.content_type,
            }
        except RenderedContent.DoesNotExist:
            return None

    def get_from_s3(self, content_path):
        result = get_content_from_s3(key=content_path)
        if result and result.get("content"):
            content = result.get("content")
            content_type = result.get("content_type")
            if content_type == "text/asciidoc":
                result["content"] = self.convert_adoc_to_html(content)
            return result

    def get_template_names(self):
        """Returns the template name."""
        content_type = self.content_dict.get("content_type")
        if content_type == "text/asciidoc":
            return [self.template_name]
        return []

    def render_to_response(self, context, **response_kwargs):
        """Return the HTML response with a template, or just the content directly."""
        if self.get_template_names():
            return super().render_to_response(context, **response_kwargs)
        else:
            return HttpResponse(
                context["content"], content_type=context["content_type"]
            )

    def save_to_database(self, cache_key, result):
        """Saves the rendered asciidoc content to the database via celery."""
        content_type = result.get("content_type")
        last_updated_at_raw = result.get("last_updated_at")

        if content_type == "text/asciidoc":
            last_updated_at_raw = result.get("last_updated_at")
            last_updated_at = (
                parse(last_updated_at_raw) if last_updated_at_raw else None
            )
            save_rendered_content.delay(
                cache_key,
                content_type,
                result["content"],
                last_updated_at=last_updated_at,
            )

    def convert_adoc_to_html(self, content):
        """Renders asciidoc content to HTML."""
        return process_adoc_to_html_content(content)


def antora_header_view(request):
    print(request.user)
    print(request.headers)
    return render(request, "includes/_header.html")


def antora_footer_view(request):
    print(request.user)
    print(request.headers)
    return render(request, "includes/_footer.html")
