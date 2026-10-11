"""Views stub do OS Service — endpoints entram com a extração real."""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def ordens_servico(request):
    """Stub: listagem/criação de OS. Extração real vem em seguida."""
    return Response({'detail': 'Endpoint em construção (Fase 4)'})
