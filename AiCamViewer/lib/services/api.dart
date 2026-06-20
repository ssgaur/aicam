import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:dio/dio.dart';
import '../models/clip.dart';

class AicamApi {
  String baseUrl;

  AicamApi({required this.baseUrl});

  http.Client get _client {
    // Trust self-signed certs
    final httpClient = HttpClient()
      ..badCertificateCallback = (cert, host, port) => true;
    return _IOClient(httpClient);
  }

  Future<List<Clip>> getClips({int minutes = 60}) async {
    final url = '$baseUrl/api/native/clips?minutes=$minutes';
    final resp = await _client.get(Uri.parse(url));
    if (resp.statusCode != 200) throw Exception('Failed: ${resp.statusCode}');
    final data = jsonDecode(resp.body);
    final list = data['clips'] as List;
    return list.map((j) => Clip.fromJson(j as Map<String, dynamic>)).toList();
  }

  Future<ClipRange> getRange() async {
    final url = '$baseUrl/api/native/clips/range';
    final resp = await _client.get(Uri.parse(url));
    if (resp.statusCode != 200) throw Exception('Failed: ${resp.statusCode}');
    return ClipRange.fromJson(jsonDecode(resp.body));
  }

  Future<List<Clip>> getClipsByRange(double start, double end) async {
    final url = '$baseUrl/api/native/clips?start=$start&end=$end';
    final resp = await _client.get(Uri.parse(url));
    if (resp.statusCode != 200) throw Exception('Failed: ${resp.statusCode}');
    final data = jsonDecode(resp.body);
    final list = data['clips'] as List;
    return list.map((j) => Clip.fromJson(j as Map<String, dynamic>)).toList();
  }

  String thumbUrl(int clipId) => '$baseUrl/media/clip/$clipId/thumb';
  String videoUrl(int clipId) => '$baseUrl/media/clip/$clipId';

  Future<bool> deleteClip(int clipId) async {
    final url = '$baseUrl/api/native/clips/$clipId';
    final resp = await _client.delete(Uri.parse(url));
    return resp.statusCode == 200;
  }

  Future<String?> downloadClip(int clipId) async {
    final dir = await getApplicationDocumentsDirectory();
    final path = '${dir.path}/aicam_clip_$clipId.mp4';
    final dio = Dio()
      ..httpClientAdapter
      ..options.followRedirects = true;
    // Trust self-signed certs for dio
    (dio.httpClientAdapter as dynamic);
    try {
      await dio.download(
        videoUrl(clipId),
        path,
        options: Options(followRedirects: true, maxRedirects: 5),
      );
      return path;
    } catch (e) {
      return null;
    }
  }

  Future<bool> healthCheck() async {
    try {
      final resp = await _client
          .get(Uri.parse('$baseUrl/healthz'))
          .timeout(const Duration(seconds: 5));
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
}

/// Custom IOClient that trusts self-signed certificates
class _IOClient extends http.BaseClient {
  final HttpClient _inner;

  _IOClient(this._inner);

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final ioRequest = await _inner.openUrl(request.method, request.url);
    request.headers.forEach((k, v) => ioRequest.headers.set(k, v));
    if (request is http.Request && request.body.isNotEmpty) {
      ioRequest.add(utf8.encode(request.body));
    }
    final resp = await ioRequest.close();
    final headers = <String, String>{};
    resp.headers.forEach((k, values) => headers[k] = values.join(','));
    // Handle redirects for media endpoints
    if (resp.statusCode == 307 || resp.statusCode == 302) {
      final location = resp.headers.value('location');
      if (location != null) {
        final redirectClient = HttpClient()
          ..badCertificateCallback = (cert, host, port) => true;
        final redirectReq = await redirectClient.getUrl(Uri.parse(location));
        final redirectResp = await redirectReq.close();
        final redirectHeaders = <String, String>{};
        redirectResp.headers
            .forEach((k, values) => redirectHeaders[k] = values.join(','));
        return http.StreamedResponse(
          redirectResp,
          redirectResp.statusCode,
          headers: redirectHeaders,
          contentLength: redirectResp.contentLength,
        );
      }
    }
    return http.StreamedResponse(
      resp,
      resp.statusCode,
      headers: headers,
      contentLength: resp.contentLength,
    );
  }
}
