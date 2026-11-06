# SCADA académico – HMI interactivas por tanque

## Cambios de esta versión
- El P&ID del Overview permite hacer clic en T-101, T-102, T-103 y T-104 para abrir la HMI individual correspondiente.
- TK-105 también es seleccionable y abre una HMI de supervisión propia (sin lazo PID).
- Cada HMI dispone de una vista ampliada del tanque en la columna izquierda y de variables, mandos, ganancias PID y alarmas en la columna derecha.
- Los botones del menú lateral siguen disponibles como alternativa de navegación.
- Las vistas del tanque se actualizan desde el mismo estado del proceso, por lo que mantienen sincronizados los valores.

## Instalación
Desde esta carpeta:

```bash
python -m pip install -r requirements.txt
python main_qt_hibrido.py
```

## Notas
- El clic se detecta usando las coordenadas lógicas del P&ID y sus áreas sensibles, incluso cuando el widget cambia de tamaño.
- T-101…T-104 mantienen sus cuatro controladores existentes. TK-105 es una pantalla de supervisión de LT-105, no un quinto PID.
- La interfaz y las órdenes software son académicas. No constituyen un sistema de seguridad ni una parada de emergencia certificada.
