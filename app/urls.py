"""URLs raiz do OS Service."""
from django.http import JsonResponse
from django.db import DatabaseError, connection
from django.urls import include, path
from django.views.decorators.http import require_GET


@require_GET
def health_live(request):
    """Processo respondendo."""
    return JsonResponse({'status': 'ok'})


@require_GET
def health_ready(request):
    """Processo + banco acessível."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ready'})


urlpatterns = [
    path('health/live/', health_live, name='health-live'),
    path('health/ready/', health_ready, name='health-ready'),
    path('api/v1/', include('os_service.urls')),
]
