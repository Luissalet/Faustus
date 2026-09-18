// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "faustus-mlx-image-bridge",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "faustus-mlx-inpaint", targets: ["FaustusMLXInpaint"]),
        .executable(name: "faustus-mlx-colorize", targets: ["FaustusMLXColorize"]),
    ],
    dependencies: [
        .package(url: "https://github.com/xocialize/mlx-lama-swift", branch: "main"),
        .package(url: "https://github.com/xocialize/mlx-ddcolor-swift", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "FaustusMLXInpaint",
            dependencies: [
                .product(name: "LaMa", package: "mlx-lama-swift"),
                .product(name: "MIGAN", package: "mlx-lama-swift"),
            ]
        ),
        .executableTarget(
            name: "FaustusMLXColorize",
            dependencies: [
                .product(name: "DDColor", package: "mlx-ddcolor-swift"),
            ]
        ),
    ]
)
