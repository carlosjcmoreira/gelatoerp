# Services layer — business logic between HTTP routes and the database.
# Routes should call services; services call db.* modules.
# This keeps routes thin and business logic testable.


class ServiceError(Exception):
    """
    Raised by service functions when a user-visible error occurs.
    The message is safe to show directly in a flash() call.

    Usage in a route:
        try:
            result = svc.do_something(data)
        except ServiceError as e:
            flash(str(e), 'error')
            return redirect(...)
    """
