from __future__ import annotations

from .models import ProcessingJob, SourceDocument
from .services import JobHandler, RetryableJobError, register_handler
from .state import transition_document
from .storage import safe_document_path


class DocumentPipelineError(RetryableJobError):
    def __init__(self, message: str, *, code: str = "DOCUMENT_PROCESSING_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _document_for_job(job: ProcessingJob) -> SourceDocument:
    document = SourceDocument.objects.filter(pk=job.document_id, user=job.user).first()
    if document is None:
        raise DocumentPipelineError(
            "The processing document was not found.", code="DOCUMENT_NOT_FOUND"
        )
    return document


@register_handler("process_document")
def process_document_job(job: ProcessingJob) -> None:
    document = _document_for_job(job)
    try:
        transition_document(document.pk, user=job.user, status=SourceDocument.Status.VALIDATING)
        try:
            path = safe_document_path(document.pk, document.temporary_path)
        except ValueError as exc:
            raise DocumentPipelineError(
                "The temporary screenshot path is invalid.", code="TEMP_PATH_INVALID"
            ) from exc
        if not path.is_file():
            raise DocumentPipelineError(
                "The temporary screenshot is missing.", code="TEMP_FILE_MISSING"
            )

        transition_document(document.pk, user=job.user, status=SourceDocument.Status.PREPROCESSING)
        transition_document(document.pk, user=job.user, status=SourceDocument.Status.OCR_RUNNING)
        transition_document(document.pk, user=job.user, status=SourceDocument.Status.PARSING)
        transition_document(
            document.pk, user=job.user, status=SourceDocument.Status.READY_FOR_REVIEW
        )
    except DocumentPipelineError as exc:
        current = SourceDocument.objects.get(pk=document.pk)
        if current.processing_status != SourceDocument.Status.FAILED:
            transition_document(
                document.pk,
                user=job.user,
                status=SourceDocument.Status.FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )
        raise


# Keep a named handler type available to integration checks that inspect the registry.
PROCESS_DOCUMENT_HANDLER: JobHandler = process_document_job
