from django import forms

from personal.models import Empleado

from .models import Incidencia


class RevisionIncidenciaForm(forms.Form):
    categoria = forms.ChoiceField(choices=Incidencia.CATEGORIAS, label='Categoría')
    prioridad = forms.ChoiceField(choices=Incidencia.URGENCIAS, label='Prioridad')
    costo_estimado_min = forms.DecimalField(
        max_digits=10, decimal_places=2, required=False, label='Costo mínimo',
    )
    costo_estimado_max = forms.DecimalField(
        max_digits=10, decimal_places=2, required=False, label='Costo máximo',
    )
    moneda = forms.CharField(max_length=3, initial='BOB')
    tiempo_estimado_horas = forms.IntegerField(
        min_value=1, required=False, label='Tiempo estimado (horas)',
    )
    empleado = forms.ModelChoiceField(
        queryset=Empleado.objects.none(), required=False, label='Técnico asignado',
        empty_label='Sin técnico asignado',
    )
    comentario = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
        label='Motivo del ajuste',
    )

    def __init__(self, *args, incidencia, revision, puede_asignar=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.puede_asignar = puede_asignar
        edificio = incidencia.residente.vivienda.edificio
        self.fields['empleado'].queryset = Empleado.objects.filter(
            edificio=edificio, activo=True,
        ).select_related('usuario', 'puesto')
        if not puede_asignar:
            self.fields.pop('empleado')

        if not self.is_bound:
            self.initial.update({
                'categoria': revision.categoria,
                'prioridad': revision.prioridad,
                'costo_estimado_min': revision.costo_estimado_min,
                'costo_estimado_max': revision.costo_estimado_max,
                'moneda': revision.moneda,
                'tiempo_estimado_horas': revision.tiempo_estimado_horas,
                'empleado': incidencia.empleado_asignado_id,
            })

        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs['class'] = 'form-select'
            else:
                field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned = super().clean()
        minimum = cleaned.get('costo_estimado_min')
        maximum = cleaned.get('costo_estimado_max')
        if minimum is not None and maximum is not None and maximum < minimum:
            self.add_error(
                'costo_estimado_max',
                'El costo máximo debe ser mayor o igual al mínimo.',
            )
        return cleaned


class SolicitarRevisionForm(forms.Form):
    comentario = forms.CharField(
        required=True,
        min_length=5,
        label='¿Qué debe revisarse?',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Describe el cambio o la información que falta.',
        }),
    )
