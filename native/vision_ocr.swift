import Foundation
import Vision
import ImageIO

struct OCRBox: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: vision_ocr IMAGE")
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("cannot open image: \(url.path)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
request.minimumTextHeight = 0.008

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
} catch {
    fail("OCR failed: \(error.localizedDescription)")
}

let results: [OCRBox] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    return OCRBox(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.minX,
        y: 1.0 - box.maxY,
        width: box.width,
        height: box.height
    )
}.sorted {
    if abs($0.y - $1.y) > 0.008 { return $0.y < $1.y }
    return $0.x < $1.x
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
do {
    FileHandle.standardOutput.write(try encoder.encode(results))
} catch {
    fail("cannot encode OCR result: \(error.localizedDescription)")
}
