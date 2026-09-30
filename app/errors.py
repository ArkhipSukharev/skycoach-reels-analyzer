class PipelineError(Exception):
    """Ошибка обработки ролика с кодом и понятным менеджеру сообщением."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def not_found() -> PipelineError:
    return PipelineError("not_found", "Ролик не найден: он удалён или ссылка неверная.")


def private() -> PipelineError:
    return PipelineError(
        "private",
        "Ролик недоступен: аккаунт приватный или доступ к публикации ограничен.",
    )
