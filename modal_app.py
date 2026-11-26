"""Deploy with `modal deploy modal_app.py`."""
import modal

image = (modal.Image.from_registry('ubuntu:24.04', add_python='3.12')
         .apt_install('p7zip-full')
         .uv_pip_install('fastapi==0.115.12', 'a2wsgi==1.10.10', 'WsgiDAV==4.3.3')
         .add_local_python_source('modalfiles'))
app = modal.App('modalfiles', image=image)
volume = modal.Volume.from_name('modalfiles-data', create_if_missing=True)


@app.function(volumes={'/data': volume},
              secrets=[modal.Secret.from_name('modalfiles-auth')],
              cpu=4, max_containers=1, timeout=3600)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def web():
    import os
    from modalfiles.service import create_app
    return create_app('/data', os.environ['MODALFILES_USER'],
                      os.environ['MODALFILES_PASSWORD'], volume)
