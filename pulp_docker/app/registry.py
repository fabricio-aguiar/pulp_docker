import logging
import os

from aiohttp import web
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from multidict import MultiDict

from pulpcore.plugin.content import Handler, PathNotResolved
from pulpcore.plugin.models import ContentArtifact
from pulp_docker.app.models import DockerDistribution, ManifestTag, MEDIA_TYPE


log = logging.getLogger(__name__)


class ArtifactNotFound(Exception):
    """
    The artifact associated with a published-artifact does not exist.
    """

    pass


class Registry(Handler):
    """
    A set of handlers for the Docker v2 API.
    """

    distribution_model = DockerDistribution

    @staticmethod
    async def get_accepted_media_types(request):
        """
        Returns a list of media types from the Accept headers.

        Args:
            request(:class:`~aiohttp.web.Request`): The request to extract headers from.

        Returns:
            List of media types supported by the client.

        """
        accepted_media_types = []
        for header, values in request.raw_headers:
            if header == b'Accept':
                values = [v.strip().decode('UTF-8') for v in values.split(b",")]
                accepted_media_types.extend(values)
        return accepted_media_types

    @staticmethod
    def _base_paths(path):
        """
        Get a list of base paths used to match a distribution.

        Args:
            path (str): The path component of the URL.

        Returns:
            list: Of base paths.

        """
        return [path]

    @staticmethod
    async def _dispatch(path, headers):
        """
        Stream a file back to the client.

        Stream the bits.

        Args:
            path (str): The fully qualified path to the file to be served.
            headers (dict):

        Returns:
            StreamingHttpResponse: Stream the requested content.

        """
        full_headers = MultiDict()

        full_headers['Content-Type'] = headers['Content-Type']
        full_headers['Docker-Content-Digest'] = headers['Docker-Content-Digest']
        full_headers['Docker-Distribution-API-Version'] = 'registry/2.0'
        full_headers['Content-Length'] = os.path.getsize(path)
        full_headers['Content-Disposition'] = 'attachment; filename={n}'.format(
            n=os.path.basename(path))
        file_response = web.FileResponse(path, headers=full_headers)
        return file_response

    @staticmethod
    async def serve_v2(request):
        """
        Handler for Docker Registry v2 root.

        The docker client uses this endpoint to discover that the V2 API is available.
        """
        return web.json_response({})

    async def tags_list(self, request):
        """
        Handler for Docker Registry v2 tags/list API.
        """
        path = request.match_info['path']
        distribution = self._match_distribution(path)
        tags = {'name': path, 'tags': set()}
        repository_version = distribution.get_repository_version()
        for c in repository_version.content:
            c = c.cast()
            if isinstance(c, ManifestTag):
                tags['tags'].add(c.name)
        tags['tags'] = list(tags['tags'])
        return web.json_response(tags)

    async def get_tag(self, request):
        """
        Match the path and stream either Manifest or ManifestList.

        Args:
            request(:class:`~aiohttp.web.Request`): The request to prepare a response for.

        Raises:
            PathNotResolved: The path could not be matched to a published file.
            PermissionError: When not permitted.

        Returns:
            :class:`aiohttp.web.StreamResponse` or :class:`aiohttp.web.FileResponse`: The response
                streamed back to the client.

        """
        path = request.match_info['path']
        tag_name = request.match_info['tag_name']
        distribution = self._match_distribution(path)
        repository_version = distribution.get_repository_version()
        accepted_media_types = await Registry.get_accepted_media_types(request)

        try:
            tag = ManifestTag.objects.get(
                pk__in=repository_version.content,
                name=tag_name,
            )
        except ObjectDoesNotExist:
            raise PathNotResolved(tag_name)

        if tag.tagged_manifest.media_type == MEDIA_TYPE.MANIFEST_V1:
            return_media_type = MEDIA_TYPE.MANIFEST_V1_SIGNED

        elif tag.tagged_manifest.media_type in accepted_media_types:
            return_media_type = tag.tagged_manifest.media_type
        else:
            # This is where we could eventually support on-the-fly conversion to schema 1.
            log.warn(
                "The requested tag `{name}` is of type {media_type}, but the client only accepts "
                "{accepted_media_types}.".format(
                    name=tag.name,
                    media_type=tag.tagged_manifest.media_type,
                    accepted_media_types=accepted_media_types
                )
            )
            raise PathNotResolved(tag_name)

        response_headers = {'Content-Type': return_media_type,
                            'Docker-Content-Digest': tag.tagged_manifest.digest}
        return await Registry.dispatch_tag(tag, response_headers)

    @staticmethod
    async def dispatch_tag(tag, response_headers):
        """
        Finds an artifact associated with a Tag and sends it to the client.

        Args:
            tag: Either a ManifestTag or ManifestListTag
            response_headers (dict): dictionary that contains the 'Content-Type' header to send
                with the response

        Returns:
            :class:`aiohttp.web.StreamResponse` or :class:`aiohttp.web.FileResponse`: The response
                streamed back to the client.

        """
        try:
            artifact = tag._artifacts.get()
        except ObjectDoesNotExist:
            raise ArtifactNotFound(tag.name)
        else:
            return await Registry._dispatch(os.path.join(settings.MEDIA_ROOT, artifact.file.name),
                                            response_headers)

    async def get_by_digest(self, request):
        """
        Return a response to the "GET" action.
        """
        path = request.match_info['path']
        digest = "sha256:{digest}".format(digest=request.match_info['digest'])
        distribution = self._match_distribution(path)
        repository_version = distribution.get_repository_version()
        log.info(digest)
        try:
            ca = ContentArtifact.objects.get(content__in=repository_version.content,
                                             relative_path=digest)
            headers = {'Content-Type': ca.content.cast().media_type,
                       'Docker-Content-Digest': ca.content.cast().digest}
        except ObjectDoesNotExist:
            raise PathNotResolved(path)
        else:
            artifact = ca.artifact
            if artifact:
                return await Registry._dispatch(os.path.join(settings.MEDIA_ROOT,
                                                             artifact.file.name),
                                                headers)
            else:
                return await self._stream_content_artifact(request, web.StreamResponse(), ca)
