import AppKit
import Foundation
import Vision

struct OCRRecord: Codable {
    let file: String
    let lines: [String]
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: ocr_screenshots DIRECTORY\n".utf8))
    exit(2)
}

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let files = try FileManager.default.contentsOfDirectory(
    at: directory,
    includingPropertiesForKeys: nil
).filter { $0.pathExtension.lowercased() == "png" }
 .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }

let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]

for file in files {
    autoreleasepool {
        guard let image = NSImage(contentsOf: file),
              let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let cgImage = bitmap.cgImage else {
            return
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.usesCPUOnly = true
        let handler = VNImageRequestHandler(cgImage: cgImage)
        do {
            try handler.perform([request])
        } catch {
            FileHandle.standardError.write(Data("\(file.lastPathComponent): \(error)\n".utf8))
        }

        let observations = (request.results ?? []).sorted {
            let verticalDifference = abs($0.boundingBox.midY - $1.boundingBox.midY)
            if verticalDifference > 0.015 {
                return $0.boundingBox.midY > $1.boundingBox.midY
            }
            return $0.boundingBox.minX < $1.boundingBox.minX
        }
        let lines = observations.compactMap { $0.topCandidates(1).first?.string }
        let record = OCRRecord(file: file.lastPathComponent, lines: lines)
        if let data = try? encoder.encode(record) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }
}
