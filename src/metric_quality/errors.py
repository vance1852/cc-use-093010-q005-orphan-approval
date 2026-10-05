"""统计样本质量服务向 API 和命令行暴露的稳定错误。"""


class MetricQualityError(RuntimeError):
    code = "metric_quality_error"
    status = 400


class NotFound(MetricQualityError):
    code = "not_found"
    status = 404


class Conflict(MetricQualityError):
    code = "conflict"
    status = 409


class InvalidState(MetricQualityError):
    code = "invalid_state"
    status = 409


class ValidationFailed(MetricQualityError):
    code = "validation_failed"
    status = 422
