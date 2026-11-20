# Maintainer: Swaggyxren
pkgname=trawl
pkgver=0.1.0
pkgrel=1
pkgdesc="Pull selected files out of remote ZIP, RAR and ISO archives without downloading the archive"
arch=('any')
url="https://github.com/Swaggyxren/trawl"
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'libadwaita')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=(
  'python-compression: read zstd-compressed ZIP entries (Python 3.14+)'
)
source=("${pkgname}-${pkgver}.tar.gz")
sha256sums=('SKIP')

build() {
  cd "${srcdir}/${pkgname}-${pkgver}"
  python -m build --wheel --no-isolation
}

package() {
  cd "${srcdir}/${pkgname}-${pkgver}"
  python -m installer --destdir="${pkgdir}" dist/*.whl

  install -Dm644 data/io.github.Swaggyxren.Trawl.desktop \
    "${pkgdir}/usr/share/applications/io.github.Swaggyxren.Trawl.desktop"
  install -Dm644 data/io.github.Swaggyxren.Trawl.metainfo.xml \
    "${pkgdir}/usr/share/metainfo/io.github.Swaggyxren.Trawl.metainfo.xml"
  install -Dm644 data/icons/hicolor/scalable/apps/io.github.Swaggyxren.Trawl.svg \
    "${pkgdir}/usr/share/icons/hicolor/scalable/apps/io.github.Swaggyxren.Trawl.svg"
  install -Dm644 data/icons/hicolor/256x256/apps/io.github.Swaggyxren.Trawl.png \
    "${pkgdir}/usr/share/icons/hicolor/256x256/apps/io.github.Swaggyxren.Trawl.png"
  install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
}
