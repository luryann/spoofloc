class SpooflocError(Exception):
    pass


class TunnelError(SpooflocError):
    pass


class TunnelDownError(TunnelError):
    pass


class DeviceNotFoundError(SpooflocError):
    pass


class LocationError(SpooflocError):
    pass


class RouteError(SpooflocError):
    pass


class GeocodeError(SpooflocError):
    pass


class RoutingError(SpooflocError):
    pass


class ConfigError(SpooflocError):
    pass
