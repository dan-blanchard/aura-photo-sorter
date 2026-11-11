// Reads the shared album for the photo sorter. PhotoKit can't add to a shared album, so this
// only reads. Images come at full size, from iCloud if they aren't on this Mac (as with Optimize Mac
// Storage).
//
// Also reads Photos' people labels, because under launchd only this signed helper can open its
// database.
//
// Usage: aura-photo-album list <album title>            print the album's photos as JSON
//        aura-photo-album export <album title> <id> <out.jpg>
//        aura-photo-album labels <album title> <taken> <out dir>
//            for library photos taken within 2 s, write small copies and print their people
import AppKit
import Foundation
import Photos
import SQLite3

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("aura-photo-album: \(message)\n".utf8))
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: aura-photo-album list|export <album title> ...") }

let access = DispatchSemaphore(value: 0)
var status = PHAuthorizationStatus.notDetermined
PHPhotoLibrary.requestAuthorization(for: .readWrite) {
    status = $0
    access.signal()
}
access.wait()
guard status == .authorized else {
    fail("no Photos access; allow it in System Settings > Privacy & Security > Photos")
}

var album: PHAssetCollection?
PHAssetCollection.fetchAssetCollections(with: .album, subtype: .albumCloudShared, options: nil)
    .enumerateObjects { collection, _, _ in
        if collection.localizedTitle == args[2] { album = collection }
    }
guard let album else { fail("no shared album with that title") }
let assets = PHAsset.fetchAssets(in: album, options: nil)

func shortID(_ asset: PHAsset) -> String {
    asset.localIdentifier.split(separator: "/").first.map(String.init) ?? asset.localIdentifier
}

func saveJPEG(_ asset: PHAsset, size: CGSize, to path: String) -> Bool {
    let options = PHImageRequestOptions()
    options.isNetworkAccessAllowed = true
    options.isSynchronous = true
    options.deliveryMode = .highQualityFormat
    options.resizeMode = size == PHImageManagerMaximumSize ? .none : .fast
    var saved = false
    PHImageManager.default().requestImage(
        for: asset, targetSize: size, contentMode: .default, options: options
    ) { image, _ in
        guard let image, let tiff = image.tiffRepresentation,
            let bitmap = NSBitmapImageRep(data: tiff),
            let jpeg = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.92])
        else { return }
        saved = FileManager.default.createFile(atPath: path, contents: jpeg)
    }
    return saved
}

// Library photos taken within 2 s of this time, with the people Photos named on each.
func libraryPeople(taken: Double) -> [String: [String]] {
    let path = NSHomeDirectory() + "/Pictures/Photos Library.photoslibrary/database/Photos.sqlite"
    var db: OpaquePointer?
    guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        fail("can't open Photos' database")
    }
    defer { sqlite3_close(db) }
    sqlite3_busy_timeout(db, 10000)
    let sql = """
        select a.ZUUID, coalesce(nullif(p.ZDISPLAYNAME, ''), p.ZFULLNAME) from ZASSET a
        left join ZDETECTEDFACE f on f.ZASSETFORFACE = a.Z_PK and f.ZHIDDEN = 0
        left join ZPERSON p on p.Z_PK = f.ZPERSONFORFACE
        where a.ZCOLLECTIONSHARE is null and a.ZTRASHEDSTATE = 0
        and a.ZDATECREATED between ?1 and ?2
        """
    var query: OpaquePointer?
    guard sqlite3_prepare_v2(db, sql, -1, &query, nil) == SQLITE_OK else {
        fail("can't query Photos' database")
    }
    defer { sqlite3_finalize(query) }
    let appleTime = taken - 978307200
    sqlite3_bind_double(query, 1, appleTime - 2)
    sqlite3_bind_double(query, 2, appleTime + 2)
    var people = [String: [String]]()
    while sqlite3_step(query) == SQLITE_ROW {
        let id = String(cString: sqlite3_column_text(query, 0))
        var names = people[id] ?? []
        if let name = sqlite3_column_text(query, 1) { names.append(String(cString: name)) }
        people[id] = names
    }
    return people
}

switch args[1] {
case "list":
    var rows = [[String: Any]]()
    assets.enumerateObjects { asset, _, _ in
        rows.append([
            "id": shortID(asset),
            "taken": asset.creationDate?.timeIntervalSince1970 ?? 0,
            "video": asset.mediaType == .video,
        ])
    }
    let data = try! JSONSerialization.data(withJSONObject: rows)
    FileHandle.standardOutput.write(data)
case "export":
    guard args.count == 5 else { fail("usage: aura-photo-album export <album title> <id> <out.jpg>") }
    var target: PHAsset?
    assets.enumerateObjects { asset, _, stop in
        if shortID(asset) == args[3] {
            target = asset
            stop.pointee = true
        }
    }
    guard let target else { fail("no such photo in the album") }
    if !saveJPEG(target, size: PHImageManagerMaximumSize, to: args[4]) {
        fail("couldn't get the image")
    }
case "labels":
    guard args.count == 5, let taken = Double(args[3]) else {
        fail("usage: aura-photo-album labels <album title> <taken> <out dir>")
    }
    var rows = [[String: Any]]()
    for (id, names) in libraryPeople(taken: taken) {
        let found = PHAsset.fetchAssets(withLocalIdentifiers: ["\(id)/L0/001"], options: nil)
        guard let asset = found.firstObject else { continue }
        let file = "\(args[4])/\(id).jpg"
        if saveJPEG(asset, size: CGSize(width: 512, height: 512), to: file) {
            rows.append(["file": file, "names": names])
        }
    }
    let data = try! JSONSerialization.data(withJSONObject: rows)
    FileHandle.standardOutput.write(data)
default:
    fail("unknown command: \(args[1])")
}
